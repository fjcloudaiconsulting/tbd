from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from pfv2 import db
from pfv2 import login


@login.user_loader
def loader_user(id):
    return User.query.get(int(id))


class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True)
    email = db.Column(db.String(120), unique=True)
    password_hash = db.Column(db.String(128))
    account = db.relationship("Account", backref="owner", lazy="dynamic")
    account_type = db.relationship("AccountType", backref="owner", lazy="dynamic")
    budget = db.relationship("Budget", backref="owner", lazy="dynamic")
    category = db.relationship("Category", backref="owner", lazy="dynamic")
    account_name = db.relationship("Transaction", backref="owner", lazy="dynamic")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class AccountType(db.Model):
    __tablename__ = "account_type"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    accounts = db.relationship("Account", backref="acct_type", lazy="dynamic")
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class Account(db.Model):
    __tablename__ = "account"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    balance = db.Column(db.Numeric(precision=8, scale=2), nullable=False)
    income = db.relationship("Transaction", backref="account", lazy="dynamic")
    acct_type_id = db.Column(db.Integer, db.ForeignKey("account_type.id"), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class Budget(db.Model):
    __tablename__ = "budget"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    month = db.Column(db.String(2), nullable=False)
    year = db.Column(db.String(4), nullable=False)
    balance = db.Column(db.Numeric(precision=8, scale=2), nullable=False)
    total = db.Column(db.Numeric(precision=8, scale=2), nullable=False)
    budget_name = db.relationship("Transaction", backref="budget", lazy="dynamic")
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class Category(db.Model):
    __tablename__ = "category"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category_name = db.relationship("Transaction", backref="category", lazy="dynamic")
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class Transaction(db.Model):
    __tablename__ = "transaction"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    nature = db.Column(db.String(20), nullable=False)
    date = db.Column(db.Date(), nullable=False)
    value = db.Column(db.Numeric(precision=8, scale=2), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=True)
    budget_id = db.Column(db.Integer, db.ForeignKey("budget.id"), nullable=True)
