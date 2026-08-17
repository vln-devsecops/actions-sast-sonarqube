"""Deliberately flawed sample used to smoke-test the SonarQube scan pipeline
in this repo's own ci.yml. Do not "fix" the issues here without also
updating README's notes on what ci.yml expects to find - the point of this
file is to reliably trip a handful of default Sonar Python rules, plus one
Security Hotspot rule (python:S4790, weak hashing algorithm).
"""
import hashlib


def weak_checksum(data):
    return hashlib.md5(data).hexdigest()


def compute_total(values, cache={}):
    unused = 42
    total = 0
    for value in values:
        if value == None:
            continue
        total = total + value
    return total


def read_first_line(path):
    try:
        with open(path) as f:
            return f.readline()
    except:
        pass
