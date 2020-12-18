#from . import db
from pfv2 import db


# class User(db.Model):
#     # primary keys are required by SQLAlchemy
#     id = db.Column(db.Integer, primary_key=True)
#     email = db.Column(db.String(100), unique=True)
#     password = db.Column(db.String(100))
#     name = db.Column(db.String(1000))

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
    acct_type_id = db.Column(db.Integer, db.ForeignKey(
        'account_type.id'), nullable=False)

