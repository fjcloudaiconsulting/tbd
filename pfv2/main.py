from flask import Blueprint, render_template, request, jsonify
# Testing the wtforms 
from pfv2.forms import AddAccountType
# from . import db

main = Blueprint('views', __name__)


@main.route('/')
def index():
    return render_template('index.html')

#### Test and Debug Views ###
@main.route('/test', methods=['GET', 'POST'])
def test():
    form = AddAccountType()
    return render_template('testform.html', form=form)

@main.route("/debug", methods=['GET', 'POST'])
def debug():
    data = request.form
    #data = request.args
    return jsonify(data)
