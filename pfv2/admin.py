from flask import Blueprint, render_template, request, redirect, url_for, flash, redirect
from flask_login import login_required
from sqlalchemy import exc
from pfv2.models import Account, AccountType
from pfv2.forms import AddAccountTypeForm, AddAccountForm
from pfv2 import db

admin = Blueprint('admin', __name__)


@admin.route('/admin')
@login_required
def adm_interface():
    return render_template('admin.html')


@admin.route('/admin/accounts', methods=['GET', 'POST'])
@admin.route('/admin/accounts/<op>', methods=['GET', 'POST'])
@admin.route('/admin/accounts/<op>/<int:id>', methods=['GET', 'POST'])
@login_required
def adm_accounts(op=None, id=None, accounts=None):
    if request.method == 'POST':

        if op is None:

            return redirect(url_for('admin.adm_accounts'))
        # Create Account Type
        elif op == "addtype":

            acct_type = request.form['account_type']
            new_type = AccountType(name=acct_type)

            # TODO: Figure out how to check the duplicates (cammel case)
            
            try:
                db.session.add(new_type)
                db.session.commit()
                flash(f"New Account Type added: {acct_type}", 'success')
                return redirect(url_for('admin.adm_accounts'))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Account Type {acct_type} already exists. Try a new name", 'danger')
                return redirect(url_for('admin.adm_accounts'))

        # Create Account
        elif op == "add":

            acct_name = request.form['account_name']
            acct_type = request.form['account_type']
            acct_balance = request.form['initial_balance']
            new_account = Account(name=acct_name,
                                    balance=acct_balance,
                                    acct_type_id=acct_type)
            try:
                db.session.add(new_account)
                db.session.commit()
                flash(f"New Account Name added: {acct_name}", 'success')
                return redirect(url_for('admin.adm_accounts'))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Account Name {acct_name} already exists. Try a new name", 'danger')
                return redirect(url_for('admin.adm_accounts'))
            
            # TODO: Figure out how to validate the selectfield
            # TODO: Figure out how to check for name duplicates (cammel case)
        
        # Save Account after edit
        elif op == "save":

            acct_name = request.form['accountName']
            acct_type = request.form['accountType']
            acct_balance = request.form['accountBalance']

            update_account = Account.query.filter_by(id=id).first()
            update_account.name = acct_name
            update_account.acct_type_id = acct_type
            update_account.balance = acct_balance
            try:
                db.session.commit()
                flash(f"New Account Name: {acct_name}", 'success')
                return redirect(url_for('admin.adm_accounts'))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Account {acct_name} already exists. Try a new name", 'danger')
                return redirect(url_for('admin.adm_accounts'))

        # Save Account Type after edit
        elif op == "savetype":

            acct_type_name = request.form['accountTypeName']
            
            update_account_type = AccountType.query.filter_by(id=id).first()
            update_account_type.name = acct_type_name
            try:
                db.session.commit()
                flash(f"New Account Type: {acct_type_name}", 'success')
                return redirect(url_for('admin.adm_accounts'))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Account {acct_type_name} already exists. Try a new name", 'danger')
                return redirect(url_for('admin.adm_accounts'))

    else:
        # Add Account
        if op == "add":
            form = AddAccountForm()
            # Populate the AddAccount form with Account Types
            account_types = [(i.id, i.name) for i in AccountType.query.all()]
            form.account_type.choices = account_types
            return render_template('add_account.html', form=form, accounts=Account.query.all())

        # Add Account Type
        elif op == "addtype":
            form = AddAccountTypeForm()
            return render_template('add_account_type.html', form=form, account_types=AccountType.query.all())

        # Remove Account
        elif op == "remove":
            Account.query.filter_by(id=id).delete()
            db.session.commit()
            flash('Account Removed', 'danger')
            return redirect(url_for('admin.adm_accounts'))

        # Remove Account Type
        elif op == "removetype":

            check_account = Account.query.filter_by(acct_type_id=id).all()

            if check_account:
                for account in check_account:
                    flash(f"Account {account.name} associated with Account Type {account.acct_type.name}.", 'danger')
                return redirect(url_for('admin.adm_accounts'))

            else:
                AccountType.query.filter_by(id=id).delete()
                db.session.commit()
                flash('Account Type Removed', 'danger')
                return redirect(url_for('admin.adm_accounts'))

        # Edit Account
        elif op == "edit":
            account_id = Account.query.filter_by(id=id).first()
            return render_template('edit_account.html', account=account_id, account_types=AccountType.query.all())

        # Edit Account Type
        elif op == "edittype":
            account_type_id = AccountType.query.filter_by(id=id).first()
            return render_template('edit_account_type.html', account_type=account_type_id)

        # Return to the main adm page if not op is informed
        else:
            return render_template('accounts.html', accounts=Account.query.all(), account_types=AccountType.query.all())

# TODO: Draft for the Budgets view
@admin.route('/admin/budgets', methods=['GET', 'POST'])
def adm_budgets():
    return render_template('budgets.html')

# TODO: Draft for the Categories view
@admin.route('/admin/categories', methods=['GET', 'POST'])
def adm_categories():
    return render_template('categories.html')
