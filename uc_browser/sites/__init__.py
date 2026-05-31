"""Site-specific automation recipes built on UCBrowser primitives.

Each module wraps a particular site's quirks (URL patterns, sidebar
selectors, menu flows) into a stateful client that reuses a single
UCBrowser instance across operations.
"""
