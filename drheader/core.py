"""Main module and entry point for analysis."""
import json
import os

import requests
import validators
from requests import structures

from drheader import report, utils
from drheader.validators import cookie_validator, directive_validator, header_validator

_CROSS_ORIGIN_HEADERS = ['cross-origin-embedder-policy', 'cross-origin-opener-policy']

with open(os.path.join(os.path.dirname(__file__), 'resources/delimiters.json')) as delimiters:
    _DELIMITERS = utils.translate_to_case_insensitive_dict(json.load(delimiters))


class Drheader:
    """Main class and entry point for analysis.

    Attributes:
        headers (CaseInsensitiveDict): The headers to analyse.
        cookies (CaseInsensitiveDict): The cookies to analyse.
        reporter (Reporter): Reporter instance that generates and holds the final report.
    """

    def __init__(self, headers=None, url=None, method='get', params=None, request_headers=None, verify=True, timeout=10):
        """Initialises a Drheader instance.

        Either headers or url must be defined. If both are defined, the value passed in headers will take priority. If
        only url is defined, the headers will be retrieved from the HTTP response from the provided URL.

        Args:
            headers (dict | str): (optional) The headers to analyse. Must be valid JSON if passed as a string.
            url (str): (optional) The URL from which to retrieve the headers.
            method (str): (optional) The HTTP verb to use when retrieving the headers. Default is 'get'.
            params (dict): (optional) Any request parameters to send when retrieving the headers.
            request_headers (dict): (optional) Any request headers to send when retrieving the headers.
            verify (bool): (optional) A flag to verify the server's TLS certificate. Default is True.
            timeout (int): (optional) Requests timeout in seconds. Default is 10s.

        Raises:
            ValueError: If neither headers nor url is provided, or if url is not a valid URL.
        """
        if not headers:
            if not url:
                raise ValueError("Nothing provided for analysis. Either 'headers' or 'url' must be defined")
            else:
                headers = _get_headers_from_url(url, method, params, request_headers, verify, timeout)
        elif isinstance(headers, str):
            headers = json.loads(headers)

        self.cookies = structures.CaseInsensitiveDict()
        self.headers = structures.CaseInsensitiveDict(headers)
        self.reporter = report.Reporter()

        for cookie in self.headers.get('set-cookie', []):
            cookie = cookie.split('=', 1)
            self.cookies[cookie[0]] = cookie[1]

    def analyze(self, rules=None, cross_origin_isolated=False):
        """Analyses headers against a drHEADer ruleset.

        Args:
            rules (dict): (optional) The rules against which to assess the headers. Default rules are used if undefined.
            cross_origin_isolated (bool): (optional) A flag to enable cross-origin isolation rules. Default is False.

        Returns:
            A list containing all the rule violations found during analysis. The report consists of individual dict
            items per header and rule. Each item in the report will detail the non-compliant header, the rule violated
            and its associated severity, and, if applicable, the observed value of the header, any expected, disallowed
            or anomalous values, and the correct delimiter. For example:
            {
                'rule': 'Referrer-Policy',
                'message': 'Value does not match security policy. Exactly one of the expected items was expected',
                'severity': 'high',
                'value': 'origin-when-cross-origin'
                'expected': ['same-origin', 'strict-origin-when-cross-origin']
            }
        """
        if not rules:
            rules = utils.translate_to_case_insensitive_dict(utils.load_rules())
        else:
            rules = utils.translate_to_case_insensitive_dict(rules)

        h_validator = header_validator.HeaderValidator(self.headers)
        d_validator = directive_validator.DirectiveValidator(self.headers)
        c_validator = cookie_validator.CookieValidator(self.cookies)

        for header, config in rules.items():
            if header.lower() in _CROSS_ORIGIN_HEADERS and not cross_origin_isolated:
                continue
            else:
                self._analyze_header(config, h_validator, header)
                if 'directives' in config and header in self.headers:
                    self._analyze_directives(config, d_validator, header)
                if 'cookies' in config and header.lower() == 'set-cookie':
                    self._analyze_cookies(config, c_validator)
        return self.reporter.report

    def _analyze_header(self, config, validator, header):
        if header.lower() != 'set-cookie':
            self._validate_rules(config, validator, header)
        elif header in self.headers:
            for cookie in self.cookies:
                self._validate_rules(config, validator, header, cookie=cookie)

    def _analyze_directives(self, config, validator, header):
        for directive, config in config['directives'].items():
            self._validate_rules(config, validator, header, directive=directive)

    def _analyze_cookies(self, config, validator):
        for cookie, config in config['cookies'].items():
            self._validate_rules(config, validator, header='Set-Cookie', cookie=cookie)

    def _validate_rules(self, config, validator, header, directive=None, cookie=None):
        if header in _DELIMITERS:
            config['delimiters'] = _DELIMITERS[header]

        is_required = str(config['required']).strip().lower()

        if is_required == 'false':
            report_item = validator.validate_not_exists(config, header, directive=directive, cookie=cookie)
            self._add_to_report_if_exists(report_item)
        else:
            exists = self._validate_exists(is_required, config, validator, header, directive, cookie)
            if exists:
                self._validate_enforced_value(config, validator, header, directive)
                self._validate_avoid_and_contain_values(config, validator, header, directive, cookie)

    def _validate_exists(self, is_required, config, validator, header, directive, cookie):
        if is_required == 'true':
            report_item = validator.validate_exists(config, header, directive=directive, cookie=cookie)
            self._add_to_report_if_exists(report_item)
            return bool(not report_item)
        elif cookie:
            return cookie in self.cookies
        elif directive:
            return directive in utils.parse_policy(self.headers[header], **_DELIMITERS[header], keys_only=True)
        elif header:
            return header in self.headers

    def _validate_enforced_value(self, config, validator, header, directive):
        if 'value' in config:
            report_item = validator.validate_value(config, header, directive=directive)
            self._add_to_report_if_exists(report_item)
        elif 'value-any-of' in config:
            report_item = validator.validate_value_any_of(config, header, directive=directive)
            self._add_to_report_if_exists(report_item)
        elif 'value-one-of' in config:
            report_item = validator.validate_value_one_of(config, header, directive=directive)
            self._add_to_report_if_exists(report_item)

    def _validate_avoid_and_contain_values(self, config, validator, header, directive, cookie):
        if 'must-avoid' in config:
            report_item = validator.validate_must_avoid(config, header, directive=directive, cookie=cookie)
            self._add_to_report_if_exists(report_item)
        if 'must-contain' in config:
            report_item = validator.validate_must_contain(config, header, directive=directive, cookie=cookie)
            self._add_to_report_if_exists(report_item)
        if 'must-contain-one' in config:
            report_item = validator.validate_must_contain_one(config, header, directive=directive, cookie=cookie)
            self._add_to_report_if_exists(report_item)

    def _add_to_report_if_exists(self, report_item):
        if report_item:
            try:
                self.reporter.add_item(report_item)
            except AttributeError:
                for item in report_item:
                    self.reporter.add_item(item)


def _get_headers_from_url(url, method, params, headers, verify, timeout):
    if not validators.url(url):
        raise ValueError(f"Cannot retrieve headers from '{url}'. The URL is malformed")

    request_object = getattr(requests, method.lower())
    response = request_object(url, data=params, headers=headers, verify=verify, timeout=timeout)
    response_headers = response.headers

    if len(response.raw.headers.getlist('Set-Cookie')) > 0:
        response_headers['set-cookie'] = response.raw.headers.getlist('Set-Cookie')
    return response_headers
