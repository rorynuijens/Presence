"""
conftest.py — pytest configuration for the Presence test suite.

Sets GDK_BACKEND=offscreen before any GTK import so that widget-level tests
can instantiate real GTK objects without a running display server.
"""
import os
os.environ.setdefault("GDK_BACKEND", "offscreen")
