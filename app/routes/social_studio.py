"""
Social Post Studio — Module 10.

Routes (all require staff or admin role):
  GET  /social                           — studio home: history + auto-publish toggles
  POST /social/generate/<post_type>      — on-demand draft generation (never auto-publishes)
  POST /social/preview/eod/<scenario>    — generate a built-in preview-only EOD draft
  POST /social/preview/eod/upload        — generate preview-only EOD from JSON/XLSX
  POST /social/theme/eod                 — save the global content-image theme
  POST /social/theme/eod/reset           — restore environment/default theme values
  GET  /social/theme/eod/logo            — serve the current managed logo
  POST /social/publish/<post_id>         — publish a draft
  POST /social/delete/<post_id>          — permanently delete a post
  POST /social/update/<post_id>          — save edited tweet text on a draft
  GET  /social/chart/<post_id>           — serve the chart image for a post
  GET  /social/media/<post_id>/<index>   — serve one EOD v2/v3 image
  POST /social/autopublish/<post_type>   — toggle auto-publish flag
  GET  /social/templates                 — list editable templates
  GET  /social/templates/<post_type>     — edit template form
  POST /social/templates/<post_type>     — save template
  POST /social/upload-charts             — upload start+end weekly charts, run vision LLM,
                                           save as weekly_chart draft
"""
import logging
import os
from pathlib import Path

from bson import ObjectId
from flask import (
    Blueprint, abort, flash, redirect, render_template,
    request, send_file, session, url_for,
)
from werkzeug.utils import secure_filename

from app.extensions import limiter, mongo
from app.models import platform_settings, post_templates, social_posts
from app.services.social_generation import (
    generate_eod_preview_draft,
    generate_draft,
    publish_post,
    validate_post_template,
    validate_post_thread,
)
from app.services.eod_preview import (
    MAX_PREVIEW_INPUT_BYTES,
    load_builtin_fixture,
    load_preview_bytes,
)
from app.services.eod_social_theme import (
    EDITABLE_FIELDS as EOD_THEME_FIELDS,
    MAX_LOGO_BYTES as EOD_THEME_MAX_LOGO_BYTES,
    get_eod_theme,
    reset_eod_theme,
    save_eod_theme,
)
from app.services.eod_social_cards import (
    MEDIA_VERSION as EOD_MEDIA_VERSION,
    cleanup_eod_media,
    validate_eod_media_assets,
)
from app.utils.decorators import role_required
from app.utils.time import now_ct

log = logging.getLogger(__name__)
social_bp = Blueprint("social", __name__, url_prefix="/social")

# Template-based post types (scheduled, use post_templates collection)
_VALID_POST_TYPES = {"premarket", "eod", "eow"}
# All post types including vision-generated
_POST_TYPE_LABELS = {
    "premarket":    "Pre-Market Brief",
    "eod":          "EOD Report",
    "eow":          "EOW Wrap",
    "weekly_chart": "Weekly Chart Analysis",
}

_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}
_UPLOAD_DIR = Path(os.environ.get("SOCIAL_CHART_DIR", "output/social_charts")) / "uploads"


# ── Studio home ────────────────────────────────────────────────────────────

@social_bp.route("")
@role_required("staff", "admin")
def studio():
    db = mongo.db
    settings = platform_settings.get_settings(db)
    eod_theme = get_eod_theme(db)
    posts = social_posts.find_recent(db, limit=50)

    # Serialize ObjectIds so Jinja can render them
    for p in posts:
        p["id"] = str(p["_id"])

    return render_template(
        "admin/social_studio.html",
        posts=posts,
        settings=settings,
        eod_theme=eod_theme,
        eod_media_version=EOD_MEDIA_VERSION,
        post_type_labels=_POST_TYPE_LABELS,
    )


# ── On-demand generation (always creates a draft, never auto-publishes) ────

@social_bp.route("/generate/<post_type>", methods=["POST"])
@role_required("staff", "admin")
def generate(post_type: str):
    if post_type not in _VALID_POST_TYPES:
        abort(404)

    try:
        post_id, chart_ok = generate_draft(mongo.db, post_type, triggered_by=session["user_id"])
        if chart_ok or post_type == "premarket":
            flash(
                f"{_POST_TYPE_LABELS[post_type]} draft generated — "
                f"review it below, then publish when ready.",
                "success",
            )
        else:
            flash(
                f"{_POST_TYPE_LABELS[post_type]} draft generated but its legacy chart failed — "
                f"check server logs before publishing.",
                "warning",
            )
    except ValueError as exc:
        flash(f"Cannot generate: {exc}", "warning")
    except Exception as exc:
        log.exception("on-demand generation failed post_type=%s", post_type)
        flash(f"Generation failed: {exc}", "danger")

    return redirect(url_for("social.studio"))


# ── Global EOD content-image theme ───────────────────────────────────────

@social_bp.route("/theme/eod", methods=["POST"])
@role_required("staff", "admin")
def save_theme():
    values = {field: request.form.get(field, "") for field in EOD_THEME_FIELDS}
    logo = request.files.get("theme_logo")
    logo_content = None
    logo_filename = ""
    if logo is not None and logo.filename:
        logo_filename = secure_filename(logo.filename)
        logo_content = logo.stream.read(EOD_THEME_MAX_LOGO_BYTES + 1)
    try:
        save_eod_theme(
            mongo.db,
            values,
            session["user_id"],
            logo_content=logo_content,
            logo_filename=logo_filename,
            clear_logo=request.form.get("clear_logo") == "on",
        )
        flash("EOD image theme saved. It will apply to newly generated drafts.", "success")
    except (ValueError, RuntimeError) as exc:
        flash(f"Cannot save EOD image theme: {exc}", "danger")
    except Exception as exc:
        log.exception("EOD image theme save failed")
        flash(f"Cannot save EOD image theme: {exc}", "danger")
    return redirect(url_for("social.studio"))


@social_bp.route("/theme/eod/reset", methods=["POST"])
@role_required("staff", "admin")
def reset_theme():
    try:
        reset_eod_theme(mongo.db, session["user_id"])
        flash("EOD image theme restored to its configured defaults.", "success")
    except Exception as exc:
        log.exception("EOD image theme reset failed")
        flash(f"Cannot reset EOD image theme: {exc}", "danger")
    return redirect(url_for("social.studio"))


@social_bp.route("/theme/eod/logo")
@limiter.exempt
@role_required("staff", "admin")
def theme_logo():
    theme = get_eod_theme(mongo.db)
    path_value = theme.get("logo_path")
    if not path_value:
        abort(404)
    path = Path(path_value)
    if not path.is_file():
        abort(404)
    return send_file(str(path.resolve()), mimetype="image/png")


def _create_preview(preview: dict) -> None:
    post_id, _ = generate_eod_preview_draft(
        mongo.db, preview, triggered_by=session["user_id"]
    )
    log.info("Manual EOD preview generated: post_id=%s", post_id)
    flash(
        "Preview-only EOD draft generated — review all five images and accessible posts below.",
        "success",
    )


@social_bp.route("/preview/eod/<scenario>", methods=["POST"])
@role_required("staff", "admin")
def preview_builtin(scenario: str):
    if scenario not in {"positive", "negative"}:
        abort(404)
    narrative_source = request.form.get("narrative_source", "file")
    try:
        _create_preview(
            load_builtin_fixture(scenario, narrative_source=narrative_source)
        )
    except (ValueError, RuntimeError) as exc:
        flash(f"Preview generation failed: {exc}", "danger")
    except Exception as exc:
        log.exception("built-in EOD preview failed scenario=%s", scenario)
        flash(f"Preview generation failed: {exc}", "danger")
    return redirect(url_for("social.studio"))


@social_bp.route("/preview/eod/upload", methods=["POST"])
@role_required("staff", "admin")
def preview_upload():
    upload = request.files.get("preview_file")
    if upload is None or not upload.filename:
        flash("Choose a JSON or Excel preview file first.", "warning")
        return redirect(url_for("social.studio"))
    filename = secure_filename(upload.filename)
    if not filename:
        flash("The preview filename is invalid.", "warning")
        return redirect(url_for("social.studio"))
    narrative_source = request.form.get("narrative_source", "file")
    try:
        content = upload.stream.read(MAX_PREVIEW_INPUT_BYTES + 1)
        preview = load_preview_bytes(
            content,
            filename,
            narrative_source=narrative_source,
        )
        _create_preview(preview)
    except (ValueError, RuntimeError) as exc:
        flash(f"Preview generation failed: {exc}", "danger")
    except Exception as exc:
        log.exception("uploaded EOD preview failed filename=%s", filename)
        flash(f"Preview generation failed: {exc}", "danger")
    return redirect(url_for("social.studio"))


# ── Manual publish ─────────────────────────────────────────────────────────

@social_bp.route("/publish/<post_id>", methods=["POST"])
@role_required("staff", "admin")
def publish(post_id: str):
    post = social_posts.find_by_id(mongo.db, post_id)
    if post is None:
        abort(404)
    if post["status"] != "draft":
        flash("Only draft posts can be published.", "warning")
        return redirect(url_for("social.studio"))

    try:
        tweet_ids = publish_post(mongo.db, post_id)
        flash(f"Published — {len(tweet_ids)} tweet(s) sent.", "success")
    except RuntimeError as exc:
        flash(f"Publish failed: {exc}", "danger")
    except Exception as exc:
        log.exception("manual publish failed post_id=%s", post_id)
        flash(f"Publish failed: {exc}", "danger")

    return redirect(url_for("social.studio"))


# ── Manual delete ─────────────────────────────────────────────────────────

@social_bp.route("/delete/<post_id>", methods=["POST"])
@role_required("staff", "admin")
def delete(post_id: str):
    post = social_posts.find_by_id(mongo.db, post_id)
    if post is None:
        abort(404)
    if social_posts.delete_by_id(mongo.db, post_id):
        if post.get("post_type") == "eod":
            cleanup_eod_media(post)
        flash("Post deleted.", "success")
    else:
        flash("Delete failed.", "danger")
    return redirect(url_for("social.studio"))


# ── Chart image server ────────────────────────────────────────────────────

@social_bp.route("/chart/<post_id>")
@limiter.exempt
@role_required("staff", "admin")
def chart_image(post_id: str):
    post = social_posts.find_by_id(mongo.db, post_id)
    if post is None or not post.get("chart_path"):
        abort(404)
    path = Path(post["chart_path"])
    if not path.is_file():
        abort(404)
    return send_file(str(path.resolve()), mimetype="image/png")


@social_bp.route("/media/<post_id>/<int:post_index>")
@limiter.exempt
@role_required("staff", "admin")
def media_image(post_id: str, post_index: int):
    post = social_posts.find_by_id(mongo.db, post_id)
    if post is None:
        abort(404)
    asset = next(
        (
            item for item in (post.get("media_assets") or [])
            if item.get("post_index") == post_index
        ),
        None,
    )
    if asset is None or not asset.get("path"):
        abort(404)
    path = Path(asset["path"])
    if not path.is_file():
        abort(404)
    return send_file(str(path.resolve()), mimetype="image/png")


# ── Tweet editor (save edited tweet text) ─────────────────────────────────

@social_bp.route("/update/<post_id>", methods=["POST"])
@role_required("staff", "admin")
def update_tweets(post_id: str):
    post = social_posts.find_by_id(mongo.db, post_id)
    if post is None:
        abort(404)
    # tweets are sent as tweet_0, tweet_1, … in form order
    tweets = []
    i = 0
    while True:
        val = request.form.get(f"tweet_{i}")
        if val is None:
            break
        text = val.strip()
        if text:
            tweets.append(text)
        i += 1
    if not tweets:
        flash("Cannot save — at least one tweet must have content.", "warning")
        return redirect(url_for("social.studio"))
    validation = None
    post_type = post.get("post_type")
    if post_type in _VALID_POST_TYPES:
        try:
            validation = validate_post_thread(
                post_type,
                tweets,
                chart_path=post.get("chart_path"),
                require_chart=False,
            )
            if post_type == "eod" and post.get("media_version") == EOD_MEDIA_VERSION:
                validation.update(validate_eod_media_assets(post.get("media_assets")))
            elif post_type == "eod" and post.get("media_version") != 2:
                validation = validate_post_thread(
                    post_type,
                    tweets,
                    chart_path=post.get("chart_path"),
                    require_chart=True,
                )
        except ValueError as exc:
            flash(f"Cannot save {_POST_TYPE_LABELS[post_type]}: {exc}", "danger")
            return redirect(url_for("social.studio"))
    social_posts.update_tweets(mongo.db, post_id, tweets, validation_result=validation)
    flash("Draft saved.", "success")
    return redirect(url_for("social.studio"))


# ── Auto-publish toggle ────────────────────────────────────────────────────

@social_bp.route("/autopublish/<post_type>", methods=["POST"])
@role_required("staff", "admin")
def toggle_autopublish(post_type: str):
    if post_type not in _VALID_POST_TYPES:
        abort(404)

    if post_type == "eod":
        flash("EOD reports always require staff/admin review and cannot auto-publish.", "warning")
        return redirect(url_for("social.studio"))

    enabled = request.form.get("enabled") == "on"
    platform_settings.set_autopublish(mongo.db, post_type, enabled, session["user_id"])
    state = "ON" if enabled else "OFF"
    flash(f"Auto-publish for {_POST_TYPE_LABELS[post_type]} is now {state}.", "success")
    return redirect(url_for("social.studio"))


# ── Template management ────────────────────────────────────────────────────

@social_bp.route("/templates")
@role_required("staff", "admin")
def templates():
    tmpl_list = post_templates.find_all(mongo.db)
    return render_template(
        "admin/social_templates.html",
        templates=tmpl_list,
        post_type_labels=_POST_TYPE_LABELS,
    )


@social_bp.route("/templates/<post_type>", methods=["GET", "POST"])
@role_required("staff", "admin")
def template_edit(post_type: str):
    if post_type not in _VALID_POST_TYPES:
        abort(404)

    db = mongo.db

    if request.method == "POST":
        text = request.form.get("template_text", "").strip()
        if not text:
            flash("Template text cannot be empty.", "danger")
        else:
            try:
                validate_post_template(post_type, text)
            except ValueError as exc:
                flash(f"Cannot save {_POST_TYPE_LABELS[post_type]} template: {exc}", "danger")
                tmpl = post_templates.get_template(db, post_type)
                return render_template(
                    "admin/social_template_edit.html",
                    post_type=post_type,
                    label=_POST_TYPE_LABELS[post_type],
                    template={**(tmpl or {}), "template_text": text},
                    tweet_sep=post_templates.TWEET_SEP,
                )
            post_templates.update_template(db, post_type, text, session["user_id"])
            flash(f"{_POST_TYPE_LABELS[post_type]} template saved.", "success")
            return redirect(url_for("social.templates"))

    tmpl = post_templates.get_template(db, post_type)
    return render_template(
        "admin/social_template_edit.html",
        post_type=post_type,
        label=_POST_TYPE_LABELS[post_type],
        template=tmpl,
        tweet_sep=post_templates.TWEET_SEP,
    )


# ── Weekly chart upload + vision analysis ─────────────────────────────────

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in _ALLOWED_EXTENSIONS


@social_bp.route("/upload-charts", methods=["POST"])
@role_required("staff", "admin")
def upload_charts():
    start_file = request.files.get("start_chart")
    end_file = request.files.get("end_chart")

    if not start_file or not end_file:
        flash("Both a start-of-week and end-of-week chart are required.", "danger")
        return redirect(url_for("social.studio"))

    if not _allowed_file(start_file.filename) or not _allowed_file(end_file.filename):
        flash("Only PNG and JPG files are accepted.", "danger")
        return redirect(url_for("social.studio"))

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ts = now_ct().strftime("%Y%m%d_%H%M%S")
    start_path = _UPLOAD_DIR / f"{ts}_start_{secure_filename(start_file.filename)}"
    end_path = _UPLOAD_DIR / f"{ts}_end_{secure_filename(end_file.filename)}"
    start_file.save(str(start_path))
    end_file.save(str(end_path))

    try:
        from app.services.vision_analysis import analyze_weekly_charts
        tweets = analyze_weekly_charts(str(start_path), str(end_path))
    except Exception as exc:
        log.exception("Vision analysis failed")
        flash(f"Vision analysis failed: {exc}", "danger")
        return redirect(url_for("social.studio"))

    doc = {
        "post_type":           "weekly_chart",
        "platform":            "twitter",
        "symbol":              "SPX",
        "status":              "draft",
        "trigger":             "manual",
        "generated_by":        ObjectId(session["user_id"]),
        "source_snapshot_ref": None,
        "tweets":              tweets,
        "chart_path":          str(end_path),  # end-of-week chart attached to tweet
    }
    post_id = social_posts.insert_draft(mongo.db, doc)
    flash(
        f"Weekly Chart Analysis draft created ({len(tweets)} tweet(s)) — review below.",
        "success",
    )
    return redirect(url_for("social.studio"))
