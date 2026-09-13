from flask_pymongo import PyMongo
from flask_session import Session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from authlib.integrations.flask_client import OAuth

mongo = PyMongo()
sess = Session()
limiter = Limiter(key_func=get_remote_address)
oauth = OAuth()
