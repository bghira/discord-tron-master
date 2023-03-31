from setuptools import setup, find_packages

setup(
    name="discord-tron-master",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "discord.py",
        "Flask",
        "Flask-RESTful",
        "Flask-OAuthlib",
        "websockets",
    ],
    entry_points={
        "console_scripts": [
            "discord-tron-master=discord_tron_master.__main__:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
    ],
)
