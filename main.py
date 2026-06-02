#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_manager_main():
	try:
		from sirena_manager.main import main as manager_main

		return manager_main
	except Exception as import_error:
		# Fallback to loading from the local checkout path to avoid package shadowing.
		manager_file = Path(__file__).resolve().parent / "sirena_manager" / "main.py"
		if not manager_file.exists():
			raise RuntimeError(
				f"Cannot locate manager entrypoint at {manager_file}"
			) from import_error

		spec = importlib.util.spec_from_file_location("sirena_manager.main", manager_file)
		if spec is None or spec.loader is None:
			raise RuntimeError(f"Cannot load module spec for {manager_file}") from import_error

		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		if not hasattr(module, "main"):
			raise RuntimeError(f"Entrypoint missing in {manager_file}: expected main()")

		return module.main


if __name__ == "__main__":
	load_main = _load_manager_main()
	load_main()
