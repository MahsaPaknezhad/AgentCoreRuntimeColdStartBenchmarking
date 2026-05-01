ROUNDS ?= 5

.PHONY: deploy experiment report clean

deploy:
	python deploy.py

experiment:
	python experiment.py --rounds $(ROUNDS)

report:
	python report.py

clean:
	python deploy.py --teardown

all: deploy experiment report
