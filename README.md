This is very early proof-of-concept work on the Commons Machinery metadata catalog. Nothing interesting here so far.

Requirements
============

* Python 2.7, virtualenv, pip
* RabbitMQ
* Node.js
* GCC (to build Redis)
* Redland and Python bindings for Redland

The catalog uses celery and redis internally. Those are built and installed locally under build/backend

Installing prerequisites
------------------------

On Ubuntu:

    sudo apt-get install rabbitmq-server python-virtualenv build-essential python-librdf

Make sure that Node.js is installed to run the frontend.

Deploying locally
=================

Run the following command to setup virtualenv under build/backend with all the required dependencies.

    sh ./bootstrap.sh

Backend
-------

To enter virtualenv (set certain environment variables) use:

    source build/backend/bin/activate

Install the backend inside virtualenv:

    cd backend
    python setup.py install
    cd ..

Run `./run_local.sh` to simultaneously start Redis and Celery, or run them separately:

    build/backend/bin/redis-server redis_local.conf
    celery -A catalog_backend worker --loglevel=info --workdir=data

Redis snapshot data as well as Redland storage data will be saved under `./data`.

It's also possible to use Redis that has been previously installed in the system.

Frontend
--------

Install all the frontend dependencies:

    cd frontend
    npm install

Run it (in a separate terminal window):

    node server.js

