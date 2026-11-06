"""Entra's method picker.

Now and then Entra answers the password with "Bevestig uw identiteit", a list of
second factors, instead of the code prompt. With no state for it the sign-in
waited out its deadline. The options here come from a copy of that screen.
"""

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from afas_declaraties import entra
from afas_declaraties.entra import CODE_METHOD, Credentials, EntraLoginError, sign_in

FIXTURE = Path(__file__).parent / "fixtures" / "entra_method_picker.html"


class _Options(HTMLParser):
    """(proof type, label) for every option on the screen."""

    def __init__(self):
        super().__init__()
        self.found, self._value, self._depth, self._text = [], None, 0, []

    def handle_starttag(self, tag, attrs):
        if self._value is not None:
            self._depth += 1
        elif (value := dict(attrs).get("data-value")) is not None:
            self._value, self._depth, self._text = value, 1, []

    def handle_endtag(self, tag):
        if self._value is not None:
            self._depth -= 1
            if not self._depth:
                self.found.append((self._value, " ".join("".join(self._text).split())))
                self._value = None

    def handle_data(self, data):
        if self._value is not None:
            self._text.append(data)


def screen_options():
    parser = _Options()
    parser.feed(FIXTURE.read_text(encoding="utf-8"))
    return parser.found


class FakeOption:
    def __init__(self, page, value, label):
        self.page, self.value, self.label = page, value, label

    def is_visible(self):
        return True

    def get_attribute(self, name):
        return self.value if name == "data-value" else None

    def inner_text(self):
        return self.label

    def click(self):
        self.page.chosen = self
        self.page.pane = "code"


class FakeHeading:
    def __init__(self, text):
        self.text = text

    def is_visible(self):
        return True

    def inner_text(self):
        return self.text


class FakePage:
    """One pane at a time, on a clock that moves only while the sign-in waits."""

    url = "https://login.microsoftonline.com/<PLACEHOLDER>/login"
    ACTIVE = {("picker", entra.SEL_METHOD_PICKER), ("code", entra.SEL_TOTP)}

    def __init__(self, pane, options=()):
        self.pane, self.clock, self.chosen, self.typed = pane, 0.0, None, {}
        self.options = [FakeOption(self, value, label) for value, label in options]

    def evaluate(self, _js, arg):
        self.clock += 0.01
        return (self.pane, arg[0]) in self.ACTIVE

    def query_selector_all(self, selector):
        if selector == entra.SEL_METHOD_OPTION:
            return self.options if self.pane == "picker" else []
        if "heading" in selector:
            return [FakeHeading("Bevestig uw identiteit" if self.pane == "picker" else "Onbekend")]
        return []

    def fill(self, selector, value):
        self.typed[selector] = value

    def input_value(self, selector):
        return self.typed.get(selector, "")

    def click(self, selector):
        if selector == entra.SEL_TOTP_VERIFY:
            self.url = "https://app.example.test/"

    def wait_for_function(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, ms):
        self.clock += ms / 1000


CREDS = Credentials(username="<PLACEHOLDER>", password="<PLACEHOLDER>", totp=lambda: "000000")


def run(page, monkeypatch):
    monkeypatch.setattr(entra, "time", SimpleNamespace(monotonic=lambda: page.clock))
    sign_in(page, CREDS)


def test_the_screen_offers_the_code_among_other_methods():
    values = [value for value, _ in screen_options()]
    assert CODE_METHOD in values
    assert len(values) > 1


def test_the_picker_gets_the_code_method_then_the_normal_code_step(monkeypatch):
    page = FakePage("picker", screen_options())
    run(page, monkeypatch)
    assert page.chosen.value == CODE_METHOD
    assert page.typed == {entra.SEL_TOTP: "000000"}
    assert page.url == "https://app.example.test/"


def test_the_label_decides_when_no_option_carries_the_proof_type(monkeypatch):
    options = [("" if value == CODE_METHOD else value, label) for value, label in screen_options()]
    page = FakePage("picker", options)
    run(page, monkeypatch)
    assert "verificatiecode" in page.chosen.label.lower()


def test_a_picker_without_the_code_method_fails_at_once(monkeypatch):
    options = [(value, label) for value, label in screen_options() if value != CODE_METHOD]
    page = FakePage("picker", options)
    # Proof types only: the SMS label is where a phone number would be.
    with pytest.raises(EntraLoginError, match=r"\(offered: PhoneAppNotification, OneWaySMS\)$"):
        run(page, monkeypatch)
    assert page.clock < 1


def test_a_pane_it_does_not_know_fails_long_before_the_deadline(monkeypatch):
    page = FakePage("unknown")
    with pytest.raises(EntraLoginError, match="does not recognise: 'Onbekend'"):
        run(page, monkeypatch)
    assert entra.STALL_SECONDS < page.clock < 60
