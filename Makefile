.PHONY: install test demo
install:
	python -m pip install -e .

test:
	python -m unittest discover -s tests -v

demo:
	awb init research demo-research --goal "Prove or refute a central conjecture"
	awb run workspaces/demo-research --provider mock --max-steps 2
