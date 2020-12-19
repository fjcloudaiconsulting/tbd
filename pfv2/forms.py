from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, SelectField, PasswordField, BooleanField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError
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
    initial_balance = StringField(label='Initial Balance', validators=[DataRequired()], render_kw={**STRING_FIELD_RENDER, "placeholder": "10.00"})
    submit = SubmitField(label='Add Account', render_kw=SUBMIT_FIELD_RENDER)

