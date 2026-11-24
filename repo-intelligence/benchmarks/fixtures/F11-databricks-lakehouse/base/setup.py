from setuptools import find_packages, setup

setup(
    name="lakehouse",
    version="0.4.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=["pyspark>=3.5", "delta-spark>=3.1"],
)
