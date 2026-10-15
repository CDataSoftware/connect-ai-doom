.PHONY: test
# The whole suite: no network, no Connect AI account and no psdoom build needed. V=1 lists each test.
test:
	python3 -m unittest discover -s tests -t tests $(if $(V),-v,)
