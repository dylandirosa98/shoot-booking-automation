release: python manage.py migrate --noinput && python manage.py collectstatic --noinput && python manage.py bootstrap_prod
web: gunicorn config.wsgi --log-file -
