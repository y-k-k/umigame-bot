deploy:
	fly deploy

secrets:
	fly secrets import < .env

logs:
	fly logs
