from flask import Blueprint, render_template, request, redirect, url_for, flash, redirect
from flask_login import login_required, current_user
from sqlalchemy import exc
from decimal import Decimal
import datetime
from pfv2.models import Account, AccountType, Budget, Category
from pfv2.forms import AddAccountTypeForm, AddAccountForm, AddBudgetForm, AddCatetoryForm
from pfv2 import db

admin = Blueprint("admin", __name__)


@admin.route("/admin")
@login_required
def adm_interface():
    return render_template("admin.html")


@admin.route("/admin/accounts", methods=["GET", "POST"])
@admin.route("/admin/accounts/<op>", methods=["GET", "POST"])
@admin.route("/admin/accounts/<op>/<int:id>", methods=["GET", "POST"])
@login_required
def adm_accounts(op=None, id=None, accounts=None):
    if request.method == "POST":
        if op is None:
            return redirect(url_for("admin.adm_accounts"))

        # Create Account Type
        elif op == "addtype":
            acct_type = request.form["account_type"]
            new_type = AccountType(name=acct_type, owner_id=current_user.id)

            # TODO: Figure out how to check the duplicates (cammel case)
            try:
                db.session.add(new_type)
                db.session.commit()
                flash(f"New Account Type added: {acct_type}", "success")
                return redirect(url_for("admin.adm_accounts"))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Account Type {acct_type} already exists. Try a new name", "danger")
                return redirect(url_for("admin.adm_accounts"))

        # Create Account
        elif op == "add":
            acct_name = request.form["account_name"]
            acct_type = request.form["account_type"]
            acct_balance = request.form["initial_balance"]
            acct_owner = current_user.id
            new_account = Account(name=acct_name, balance=acct_balance, acct_type_id=acct_type, owner_id=acct_owner)
            try:
                db.session.add(new_account)
                db.session.commit()
                flash(f"New Account Name added: {acct_name}", "success")
                return redirect(url_for("admin.adm_accounts"))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Account Name {acct_name} already exists. Try a new name", "danger")
                return redirect(url_for("admin.adm_accounts"))

            # TODO: Figure out how to validate the selectfield
            # TODO: Figure out how to check for name duplicates (cammel case)

        # Save Account after edit
        elif op == "save":
            acct_name = request.form["account_name"]
            acct_type = request.form["account_type"]
            acct_balance = request.form["initial_balance"]

            update_account = Account.query.filter_by(id=id).filter_by(owner_id=current_user.id).first()
            update_account.name = acct_name
            update_account.acct_type_id = acct_type
            update_account.balance = acct_balance

            try:
                db.session.commit()
                flash(f"Account {acct_name} updated!", "success")
                return redirect(url_for("admin.adm_accounts"))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Account {acct_name} already exists. Try a new name", "danger")
                return redirect(url_for("admin.adm_accounts"))

        # Save Account Type after edit
        elif op == "savetype":
            acct_type_name = request.form["accountTypeName"]

            update_account_type = AccountType.query.filter_by(id=id).filter_by(owner_id=current_user.id).first()
            update_account_type.name = acct_type_name
            try:
                db.session.commit()
                flash(f"New Account Type: {acct_type_name}", "success")
                return redirect(url_for("admin.adm_accounts"))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Account {acct_type_name} already exists. Try a new name", "danger")
                return redirect(url_for("admin.adm_accounts"))

    else:
        # Add Account
        if op == "add":
            form = AddAccountForm()
            # Populate the AddAccount form with Account Types
            account_types = [(i.id, i.name) for i in AccountType.query.filter_by(owner_id=current_user.id).all()]
            form.account_type.choices = account_types
            return render_template(
                "add_account.html", form=form, accounts=Account.query.filter_by(owner_id=current_user.id).all()
            )

        # Add Account Type
        elif op == "addtype":
            form = AddAccountTypeForm()
            return render_template(
                "add_account_type.html",
                form=form,
                account_types=AccountType.query.filter_by(owner_id=current_user.id).all(),
            )

        # Remove Account
        elif op == "remove":
            Account.query.filter_by(id=id).filter_by(owner_id=current_user.id).delete()
            db.session.commit()
            flash("Account Removed", "danger")
            return redirect(url_for("admin.adm_accounts"))

        # Remove Account Type
        elif op == "removetype":
            check_account = Account.query.filter_by(acct_type_id=id).filter_by(owner_id=current_user.id).all()

            if check_account:
                for account in check_account:
                    flash(f"Account {account.name} associated with Account Type {account.acct_type.name}.", "danger")
                return redirect(url_for("admin.adm_accounts"))

            else:
                AccountType.query.filter_by(id=id).delete()
                db.session.commit()
                flash("Account Type Removed", "danger")
                return redirect(url_for("admin.adm_accounts"))

        # Edit Account
        elif op == "edit":
            form = AddAccountForm()
            # Populate the AddAccount form with Account Types
            account_types = [(i.id, i.name) for i in AccountType.query.filter_by(owner_id=current_user.id).all()]
            form.account_type.choices = account_types
            form.submit.label.text = "Save"
            account = Account.query.filter_by(id=id).first()
            return render_template("edit_account.html", account=account, form=form)

        # Edit Account Type
        elif op == "edittype":
            account_type_id = AccountType.query.filter_by(id=id).first()
            return render_template("edit_account_type.html", account_type=account_type_id)

        # Return to the main adm page if not op is informed
        else:
            return render_template(
                "accounts.html",
                accounts=Account.query.filter_by(owner_id=current_user.id).all(),
                account_types=AccountType.query.filter_by(owner_id=current_user.id).all(),
            )


# TODO: Draft for the Budgets view
@admin.route("/admin/budgets", methods=["GET", "POST"])
@admin.route("/admin/budgets/<op>", methods=["GET", "POST"])
@admin.route("/admin/budgets/<op>/<int:id>", methods=["GET", "POST"])
@login_required
def adm_budgets(op=None, id=None, budgets=None):
    if request.method == "POST":
        if op is None:
            redirect(url_for("admin.adm_budgets"))

        # Create new budget
        else:
            if op == "addbudget":
                budget_name = request.form["budget_name"]
                budget_year = request.form["budget_month"].split("-")[0]
                budget_month = request.form["budget_month"].split("-")[1]
                budget_total = request.form["budget_total"]
                budget_balance = budget_total

                new_budget = Budget(
                    name=budget_name,
                    year=budget_year,
                    month=budget_month,
                    total=budget_total,
                    balance=budget_balance,
                    owner_id=current_user.id,
                )
                try:
                    db.session.add(new_budget)
                    db.session.commit()
                    flash(f"New Budget added: {budget_name}", "success")
                    return redirect(url_for("admin.adm_budgets"))
                except exc.IntegrityError:
                    db.session.rollback()
                    flash(f"Budget {budget_name} already exists. Try a new name", "danger")
                    return redirect(url_for("admin.adm_budgets"))

            # Save budget after edit
            elif op == "savebudget":
                budget_name = request.form["budget_name"]
                budget_year = request.form["budget_month"].split("-")[0]
                budget_month = request.form["budget_month"].split("-")[1]
                budget_total = Decimal(request.form["budget_total"])

                budget = Budget.query.filter_by(id=id).filter_by(owner_id=current_user.id).first()

                if budget_total > budget.total:
                    difference = budget_total - budget.total
                    new_balance = budget.balance + difference
                    budget.balance = new_balance
                elif budget_total < budget.total:
                    difference = budget.total - budget_total
                    new_balance = budget.balance - difference
                    budget.balance = new_balance
                else:
                    pass

                budget.name = budget_name
                budget.year = budget_year
                budget.month = budget_month
                budget.total = budget_total

                try:
                    db.session.commit()
                    flash(f"Budget {budget_name} updated")
                    return redirect(url_for("admin.adm_budgets"))
                except:
                    db.session.rollback()
                    # I'm raising the exception to handle it correctly
                    flash(f"Something went wrong to update the {budget_name}")
                    raise

    else:
        # Add New Budget
        if op == "addbudget":
            form = AddBudgetForm()
            return render_template(
                "add_budget.html", form=form, budgets=Budget.query.filter_by(owner_id=current_user.id).all()
            )

        # Remove Budget
        elif op == "removebudget":
            try:
                Budget.query.filter_by(id=id).filter_by(owner_id=current_user.id).delete()
                db.session.commit()
                flash(f"Budget Removed", "danger")
                return redirect(url_for("admin.adm_budgets"))
            except:
                db.session.rollback()
                # TODO: Handle the exception correctly.
                flash("Unexpected error", "danger")
                raise

        # Edit budget
        elif op == "editbudget":
            form = AddBudgetForm()
            form.submit.label.text = "Save"
            budget = Budget.query.filter_by(id=id).filter_by(owner_id=current_user.id).first()
            return render_template("edit_budget.html", budget=budget, form=form)

        # If no OP informed return the main Budget Admin page
        else:
            return render_template("budgets.html", budgets=Budget.query.filter_by(owner_id=current_user.id).all())


# TODO: Draft for the Categories view
@admin.route("/admin/categories", methods=["GET", "POST"])
@admin.route("/admin/categories/<op>", methods=["GET", "POST"])
@admin.route("/admin/categories/<op>/<int:id>", methods=["GET", "POST"])
@login_required
def adm_categories(op=None, id=None, categories=None):
    if request.method == "POST":
        if op is None:
            redirect(url_for("admin.adm_categories"))
        # Create new Category
        elif op == "addcategory":
            category_name = request.form["category_name"]
            category_owner = current_user.id
            new_category = Category(name=category_name, owner_id=category_owner)
            try:
                db.session.add(new_category)
                db.session.commit()
                flash(f"New Category added: {category_name}", "success")
                return redirect(url_for("admin.adm_categories"))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Category {category_name} already exists. Try a new name", "danger")
                return redirect(url_for("admin.adm_categories"))

        # Save Category after edit
        elif op == "savecategory":
            category_name = request.form["category_name"]
            update_category = Category.query.filter_by(id=id).filter_by(owner_id=current_user.id).first()
            update_category.name = category_name

            try:
                db.session.commit()
                flash(f"Category {category_name} updated!", "success")
                return redirect(url_for("admin.adm_categories"))
            except exc.IntegrityError:
                db.session.rollback()
                flash(f"Category {category_name} already exists. Try a new name", "danger")
                return redirect(url_for("admin.adm_accounts"))

    else:
        # Add new Category
        if op == "addcategory":
            form = AddCatetoryForm()
            return render_template(
                "add_category.html", form=form, categories=Category.query.filter_by(owner_id=current_user.id).all()
            )

        # Remove Category
        elif op == "removecategory":
            category = Category.query.filter_by(id=id).filter_by(owner_id=current_user.id).first()

            try:
                Category.query.filter_by(id=id).filter_by(owner_id=current_user.id).delete()
                db.session.commit()
                flash(f"Category {category.name} removed", "danger")
                return redirect(url_for("admin.adm_categories"))
            except:
                # TODO: Handle the exception correctly.
                db.session.rollback()
                flash("Unexpected error", "danger")
                raise

        # Edit Category
        elif op == "editcategory":
            form = AddCatetoryForm()
            # Populate the Category form with Budgets
            form.submit.label.text = "Save"
            category = Category.query.filter_by(id=id).first()
            return render_template("edit_category.html", category=category, form=form)

        else:
            return render_template(
                "categories.html", categories=Category.query.filter_by(owner_id=current_user.id).all()
            )
