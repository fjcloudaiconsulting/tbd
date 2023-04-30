from datetime import datetime


def fixdateformat(date_string):
    datetime_obj = datetime.strptime(date_string, "%Y-%m-%d")
    # date_fixed = datetime_obj.strftime("%d/%m/%Y")
    return datetime_obj