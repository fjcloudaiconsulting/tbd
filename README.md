Access the root of repository and run the following commands to initialize the database and run the application.

* pip install -r requirements
* export FLASK_APP=pfv2
* export DATABASE_URL="postgres://\<USERNAME\>:\<PASSWORD\>@\<HOST\>:\<PORT\>/\<DATABASE\>"
 - You can use any other DB like MySQL or SQLite
* flask db init
* flask db migrate -m "Initial migration."
* flask db upgrade
* flask run

I'm going to improve this in the future with instructions for database migrations and whatever more necessary.

## TODO list

- [ ] Avoid add duplicate Accounts and Account Types
- [ ] Modeling the database for the Budget
- [ ] Start draft the login interface
- [ ] Draft the index page and the admin page