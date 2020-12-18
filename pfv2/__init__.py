from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from pfv2.config import Config


db = SQLAlchemy()
migrate = Migrate()


def create_app():
    app = Flask(__name__)
    
    # Flask configuration
    app.config.from_object(Config)

    # Initializing DB
    db.init_app(app)
    # Adding support to DB migrations 
    migrate.init_app(app, db)

    
    # Importing models
    from pfv2.models import Account, AccountType

    # Blueprint for generic views in our app
    from pfv2.main import main as main_blueprint
    app.register_blueprint(main_blueprint)

    # Blueprint for admin view our app
    from pfv2.admin import admin as admin_blueprint
    app.register_blueprint(admin_blueprint)
   
    # Return the APP
    return app

