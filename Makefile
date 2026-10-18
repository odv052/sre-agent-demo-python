# make            drop logs/ and reports/, write both logs, run the agent
# make logs       write both logs (logs/chain-divide-simple.log, logs/chain-divide.log)
# make agent      run the agent over logs/
# make clean      drop logs/ and reports/
#
# Variables: make RUNS=30, make PYTHON=python3

PYTHON ?= .venv/bin/python
RUNS   ?= 20

.PHONY: demo clean logs agent

demo:
	$(MAKE) clean
	$(MAKE) logs
	$(MAKE) agent

clean:
	rm -rf logs reports

logs:
	$(PYTHON) demo/simple_logging.py $(RUNS)
	$(PYTHON) demo/improved_logging.py $(RUNS)

agent:
	$(PYTHON) -m sre_agent.agent
