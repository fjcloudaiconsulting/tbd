from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from pfv2 import db
from pfv2 import login


@login.user_loader
def loader_user(id):
    return User.query.get(int(id))


class User(UserMixin, db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), index=True, unique=True)
    email = db.Column(db.String(120), index=True, unique=True)
    password_hash = db.Column(db.String(128))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class AccountType(db.Model):
    __tablename__ = 'account_type'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    accounts = db.relationship("Account", backref='acct_type', lazy='dynamic')


class Account(db.Model):
    __tablename__ = 'account'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    balance = db.Column(db.String(100))
    acct_type_id = db.Column(db.Integer, db.ForeignKey('account_type.id'), nullable=False)
