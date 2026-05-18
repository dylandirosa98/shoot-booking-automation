web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && python manage.py bootstrap_prod && gunicorn config.wsgi --log-file -
