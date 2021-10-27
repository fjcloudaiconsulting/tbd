Access the root of repository and run the following commands to initialize the database and run the application.

* pip install -r requirements
* export FLASK_APP=pfv2
* export FLASK_ENV=development
* export DATABASE_URL="postgres://\<USERNAME\>:\<PASSWORD\>@\<HOST\>:\<PORT\>/\<DATABASE\>"
 - You can use any other DB like MySQL or SQLite
* flask db init
* flask db migrate -m "Initial migration."
* flask db upgrade
* flask run

I'm going to improve this in the future with instructions for database migrations and whatever more necessary.

## TODO list

- [ ] Code Improvement - Avoid add duplicate Accounts and Account Types (Camel case)
- [ ] Database Improvement - Modeling the database for the Annual Budget and Monthly
- [x] Start draft the login interface
- [ ] Visual Improvement - Draft the index page and the admin page
- [ ] Code Improvement - Sort the Budget query results by year (desc)
- [ ] Code - Draft the User/Profile administration interface
- [ ] Code - Implement password recovery
- [x] Code - Implement number format in the currency fields
- [ ] Visual Improvement - Rethink the NavBar menu
- [ ] Visual Improvement - Replace logout link by logout icon
- [ ] Code Improvement - deduplicate code in admin interface (create operation class and helpers)
- [ ] Test the `validate_on_submit` to replace `request.method == 'POST'`