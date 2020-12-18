from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, SelectField
from wtforms import validators, ValidationError
from pfv2 import db
from pfv2.models import Account, AccountType

STRING_FIELD_RENDER = {
    "class": "form-control",
}

SUBMIT_FIELD_RENDER = {
    "class": "btn btn-primary"
}

SELECT_FIELD_RENDER = {
    "class": "form-control"
}

class AddAccountType(FlaskForm):
    account_type = StringField(label='Account Type', validators=[validators.required()], render_kw={**STRING_FIELD_RENDER, "placeholder": "Checking"})
    submit = SubmitField(label='Add Account Type', render_kw=SUBMIT_FIELD_RENDER)


class AddAccount(FlaskForm):
    account_name = StringField(label="Account Name", validators=[validators.required()], render_kw={**STRING_FIELD_RENDER, "placeholder": "ING"})
    account_type = SelectField(label="Account Type", validators=[validators.required()], render_kw=SELECT_FIELD_RENDER)
    initial_balance = StringField(label='Initial Balance', validators=[validators.required()], render_kw={**STRING_FIELD_RENDER, "placeholder": "10.00"})
    submit = SubmitField(label='Add Account', render_kw=SUBMIT_FIELD_RENDER)
