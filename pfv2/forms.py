from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, SelectField, PasswordField, BooleanField, DateField, DecimalField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError
#from wtforms.fields.html5 import DateField
from pfv2 import db
from pfv2.models import Account, AccountType, User

STRING_FIELD_RENDER = {
    "class": "form-control",
}

SUBMIT_FIELD_RENDER = {
    "class": "btn btn-primary"
}

SELECT_FIELD_RENDER = {
    "class": "form-control"
}

MONTH_FIELD_RENDER = {
    "class": "form-control",
    "type": "month",
    "id": "monthForm",
}

DATE_FIELD_RENDER = {
    "class": "form-control",
    "type": "date",
    "id": "dateForm"
}

# Potential solution for number validateion
# https://stackoverflow.com/questions/62207106/wtford-decimal-field-format-currency-with-code
# number_usd = DecimalField("Some USD Number", use_locale=True, number_format="#,##0.00 USD;-# USD")
# Something to explore


class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "Username"})
    email = StringField('Email', validators=[DataRequired(), Email()], render_kw={**STRING_FIELD_RENDER, "placeholder": "user@example.com"})
    password = PasswordField('Password', validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "Password"})
    password2 = PasswordField( 'Repeat Password', validators=[DataRequired(), EqualTo('password')], render_kw={**STRING_FIELD_RENDER})
    submit = SubmitField('Register', render_kw=SUBMIT_FIELD_RENDER)

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user is not None:
            raise ValidationError('Please use a different username.')

    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user is not None:
            raise ValidationError('Please use a different email address.')


class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "Username"})
    password = PasswordField('Password', validators=[DataRequired()],render_kw={**STRING_FIELD_RENDER, "placeholder": "Password"})
    remember_me = BooleanField('Remember Me')
    submit = SubmitField('Sign In', render_kw=SUBMIT_FIELD_RENDER)


class AddAccountTypeForm(FlaskForm):
    account_type = StringField(label='Account Type', validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "Checking"})
    submit = SubmitField(label='Add Account Type', render_kw=SUBMIT_FIELD_RENDER)


class AddAccountForm(FlaskForm):
    account_name = StringField(label="Account Name", validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "ING"})
    account_type = SelectField(label="Account Type", validators=[DataRequired()], render_kw=SELECT_FIELD_RENDER)
    initial_balance = DecimalField(label="Initial Balance", validators=[DataRequired()], render_kw={"type":"number", "step": "0.01", "class":"form-control", "placeholder": "50.00"}, places=2, rounding=None)
    submit = SubmitField(label='Add Account', render_kw=SUBMIT_FIELD_RENDER)

class AddBudgetForm(FlaskForm):
    budget_name = StringField(label="Budget Name", validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "Leisure"})
    budget_month = DateField(label="Month", validators=[DataRequired()], render_kw=MONTH_FIELD_RENDER, format='%m-%Y')
    budget_total = DecimalField(label="Value", validators=[DataRequired()], render_kw={"type":"number", "step": "0.01", "class":"form-control", "placeholder": "50.00"}, places=2, rounding=None)
    submit = SubmitField(label='Add Budget', render_kw=SUBMIT_FIELD_RENDER)

class AddCatetoryForm(FlaskForm):
    category_name = StringField(label="Category Name", validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "Gasoline"})
    category_budget = SelectField(label="Budget Associated", validators=[DataRequired()], render_kw=SELECT_FIELD_RENDER)
    submit = SubmitField(label='Add Category', render_kw=SUBMIT_FIELD_RENDER)

class TransactionForm(FlaskForm):
    tr_type = SelectField(label="Type", validators=[DataRequired()], choices=[("income", "Income"), ("expense", "Expense"), ("transfer", "Transfer")], render_kw=SELECT_FIELD_RENDER)
    tr_desc = StringField(label="Description", validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "Kruidwat"})
    tr_date = DateField(label="Date", validators=[DataRequired()], render_kw=DATE_FIELD_RENDER)
    tr_value = DecimalField(label="Value", validators=[DataRequired()], render_kw={"type":"number", "step": "0.01", "class":"form-control", "placeholder": "50.00"}, places=2, rounding=None)
    tr_account = SelectField(label="Account", validators=[DataRequired()], render_kw=SELECT_FIELD_RENDER)
    tr_category = SelectField(label="Category", render_kw=SELECT_FIELD_RENDER)
    submit = SubmitField(label='Add Transaction', render_kw=SUBMIT_FIELD_RENDER)
