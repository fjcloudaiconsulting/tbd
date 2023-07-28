import os

basedir = os.path.abspath(os.path.dirname(__file__))


class Config(object):
    TESTING = False


# class ProductionConfig(Config):
#     SECRET_KEY = os.environ.get("SECRET_KEY")
#     SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")
#     SQLALCHEMY_TRACK_MODIFICATIONS = False


class DevelopmentConfig(Config):
    SECRET_KEY = os.environ.get("SECRET_KEY") or "you-will-never-guess"
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "sqlite:///" + os.path.join(basedir, "../pfv2.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = True
