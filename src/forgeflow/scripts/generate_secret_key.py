"""Print a new FORGEFLOW_SECRET_KEY. Usage: python -m forgeflow.scripts.generate_secret_key"""

from forgeflow.extensibility.secrets import generate_key

if __name__ == "__main__":
    print(generate_key())
