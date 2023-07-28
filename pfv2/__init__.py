from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask.logging import default_handler
from pfv2.config import DevelopmentConfig

db = SQLAlchemy()
migrate = Migrate()
login = LoginManager()


def create_app():
    app = Flask(__name__)

    # Flask configuration
    app.config.from_object(DevelopmentConfig)
    # Initializing DB
    db.init_app(app)
    # Adding support to DB migrations
    migrate.init_app(app, db)
    # Adding support to login
    login.init_app(app)
    login.login_view = "auth.login"

    # Importing models
    from pfv2.models import Account, AccountType, Budget, Category

    # Blueprint for generic views in our app
    from pfv2.main import main as main_blueprint

    app.register_blueprint(main_blueprint)

    # Blueprint for admin view our app
    from pfv2.admin import admin as admin_blueprint

    app.register_blueprint(admin_blueprint)

    # Blueprint for authentication view
    from pfv2.auth import auth as auth_blueprint

    app.register_blueprint(auth_blueprint)

    # Return the APP
    return app
