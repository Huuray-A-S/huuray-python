"""Test suite for the huuray client.

No test in here touches the network. Ordering gift cards from a test runner
would spend real money, so every request goes through the fake transport in
``tests/helpers.py``.
"""
