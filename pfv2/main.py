from flask import Blueprint, render_template, request
from flask_login import login_required

main = Blueprint('main', __name__)


@main.route('/')
def index():
    return render_template('index.html')

@main.route('/frontpage')
@login_required
def frontpage(methods = ['GET', 'POST']):
    return render_template('front_page.html')

#### Test and Debug Views ###
# @main.route('/test', methods=['GET', 'POST'])
# def test():
#     form = AddAccountForm()
#     return render_template('testform.html', form=form)

# @main.route("/debug", methods=['GET', 'POST'])
# def debug():
#     data = request.form
#     data = request.args
#     return data
