import functools
from functools import lru_cache

import logging

import requests as requests
from bs4 import BeautifulSoup
import re
import random
import time
import pickle
import os
import datetime

from frozendict import frozendict

from . import utils, cache

cache_maxsize = 64


def lru_cache_freezeargs(func):
    """
    Decorator transforms mutable dictionary and list arguments into immutable types
    Needed so lru_cache can cache method calls what has dict or list arguments.
    """

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        args = tuple([frozendict(arg) if isinstance(arg, dict) else arg for arg in args])
        kwargs = {k: frozendict(v) if isinstance(v, dict) else v for k, v in kwargs.items()}
        args = tuple([tuple(arg) if isinstance(arg, list) else arg for arg in args])
        kwargs = {k: tuple(v) if isinstance(v, list) else v for k, v in kwargs.items()}
        return func(*args, **kwargs)

    # copy over the lru_cache extra methods to this wrapper to be able to access them
    # after this decorator has been applied
    wrapped.cache_info = func.cache_info
    wrapped.cache_clear = func.cache_clear
    return wrapped


class TickerData:
    """
    Have one place to retrieve data from Yahoo API in order to ease caching and speed up operations

    Credit for code for cookie & crumb goes to StackOverflow:
    https://stackoverflow.com/questions/76065035/yahoo-finance-v7-api-now-requiring-cookies-python
    """
    user_agent_headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.95 Safari/537.36'}

    def __init__(self, ticker: str, session=None):
        self.ticker = ticker
        self._session = session or requests.Session()

        try:
            self._session.cache
        except AttributeError:
            # Not caching
            self._session_is_caching = False
        else:
            # Is caching. This is annoying. 
            # Can't simply use a non-caching session to fetch cookie & crumb, 
            # because then the caching-session won't have cookie.
            self._session_is_caching = True
            from requests_cache import DO_NOT_CACHE
            self._expire_after = DO_NOT_CACHE
        if self._session_is_caching and utils.cookie is None:
            utils.print_once("WARNING: cookie & crumb does not work well with requests_cache. Am experimenting with 'expire_after=DO_NOT_CACHE', but you need to help stress-test.")

        # Default to using 'basic' strategy
        self._cookie_strategy = 'basic'
        # If it fails, then fallback method is 'csrf'
        # self._cookie_strategy = 'csrf'

    def _toggle_cookie_strategy(self):
        if self._cookie_strategy == 'csrf':
            utils.get_yf_logger().debug(f'toggling cookie strategy {self._cookie_strategy} -> basic')
            self._session.cookies.clear()
            self._cookie_strategy = 'basic'
        else:
            utils.get_yf_logger().debug(f'toggling cookie strategy {self._cookie_strategy} -> csrf')
            self._cookie_strategy = 'csrf'
        utils.cookie = None
        utils.crumb = None

    @utils.log_indent_decorator
    def _save_session_cookies(self):
        try:
            cache.get_cookie_cache().store('csrf', self._session.cookies)
        except Exception as e:
            return False
        return True

    @utils.log_indent_decorator
    def _load_session_cookies(self):
        cookie_dict = cache.get_cookie_cache().lookup('csrf')
        if cookie_dict is None:
            return False
        # Periodically refresh, 24 hours seems fair.
        if cookie_dict['age'] > datetime.timedelta(days=1):
            return False
        self._session.cookies.update(cookie_dict['cookie'])
        utils.get_yf_logger().debug('loaded persistent cookie')

    @utils.log_indent_decorator
    def _save_cookie_basic(self, cookie):
        try:
            cache.get_cookie_cache().store('basic', cookie)
        except Exception as e:
            return False
        return True
    @utils.log_indent_decorator
    def _load_cookie_basic(self):
        cookie_dict = cache.get_cookie_cache().lookup('basic')
        if cookie_dict is None:
            return None
        # Periodically refresh, 24 hours seems fair.
        if cookie_dict['age'] > datetime.timedelta(days=1):
            return None
        utils.get_yf_logger().debug('loaded persistent cookie')
        return cookie_dict['cookie']

    @utils.log_indent_decorator
    def _get_cookie_basic(self, proxy=None, timeout=30):
        if utils.reuse_cookie and utils.cookie is not None:
            utils.get_yf_logger().debug('reusing cookie')
            return utils.cookie

        utils.cookie = self._load_cookie_basic()
        if utils.cookie is not None:
            return utils.cookie

        # To avoid infinite recursion, do NOT use self.get()
        # - 'allow_redirects' copied from @psychoz971 solution - does it help USA?
        response = self._session.get(
            url='https://fc.yahoo.com',
            headers=self.user_agent_headers,
            proxies=proxy,
            timeout=timeout,
            allow_redirects=True)

        if not response.cookies:
            utils.get_yf_logger().debug(f"response.cookies = None")
            return None
        utils.cookie = list(response.cookies)[0]
        if utils.cookie == '':
            utils.get_yf_logger().debug(f"list(response.cookies)[0] = ''")
            return None
        self._save_cookie_basic(utils.cookie)
        utils.get_yf_logger().debug(f"fetched basic cookie = {utils.cookie}")
        return utils.cookie

    @utils.log_indent_decorator
    def _get_crumb_basic(self, proxy=None, timeout=30):
        if utils.reuse_crumb and utils.crumb is not None:
            utils.get_yf_logger().debug('reusing crumb')
            return utils.crumb

        cookie = self._get_cookie_basic()
        if cookie is None:
            return None

        # - 'allow_redirects' copied from @psychoz971 solution - does it help USA?
        get_args = {
            'url': "https://query1.finance.yahoo.com/v1/test/getcrumb",
            'headers': self.user_agent_headers,
            'cookies': {cookie.name: cookie.value},
            'proxies': proxy,
            'timeout': timeout,
            'allow_redirects': True
        }
        if self._session_is_caching:
            get_args['expire_after'] = self._expire_after
            crumb_response = self._session.get(**get_args)
        else:
            crumb_response = self._session.get(**get_args)
        utils.crumb = crumb_response.text
        if utils.crumb is None or '<html>' in utils.crumb:
            return None

        utils.get_yf_logger().debug(f"crumb = '{utils.crumb}'")
        return utils.crumb
    
    @utils.log_indent_decorator
    def _get_cookie_and_crumb_basic(self, proxy, timeout):
        cookie = self._get_cookie_basic(proxy, timeout)
        crumb = self._get_crumb_basic(proxy, timeout)
        return cookie, crumb

    @utils.log_indent_decorator
    def _get_cookie_csrf(self, proxy, timeout):
        if utils.reuse_cookie and utils.cookie is not None:
            utils.get_yf_logger().debug('reusing cookie')
            return True

        elif self._load_session_cookies():
            utils.get_yf_logger().debug('reusing persistent cookie')
            utils.cookie = True
            return True

        base_args = {
            'headers': self.user_agent_headers,
            'proxies': proxy,
            'timeout': timeout}

        get_args = base_args | {'url': 'https://guce.yahoo.com/consent'}
        if self._session_is_caching:
            get_args['expire_after'] = self._expire_after
            response = self._session.get(**get_args)
        else:
            response = self._session.get(**get_args)

        soup = BeautifulSoup(response.content, 'html.parser')
        csrfTokenInput = soup.find('input', attrs={'name': 'csrfToken'})
        if csrfTokenInput is None:
            return False
        csrfToken = csrfTokenInput['value']
        utils.get_yf_logger().debug(f'csrfToken = {csrfToken}')
        sessionIdInput = soup.find('input', attrs={'name': 'sessionId'})
        sessionId = sessionIdInput['value']
        utils.get_yf_logger().debug(f"sessionId='{sessionId}")

        originalDoneUrl = 'https://finance.yahoo.com/'
        namespace = 'yahoo'
        data = {
            'agree': ['agree', 'agree'],
            'consentUUID': 'default',
            'sessionId': sessionId,
            'csrfToken': csrfToken,
            'originalDoneUrl': originalDoneUrl,
            'namespace': namespace,
        }
        post_args = base_args | {
            'url': f'https://consent.yahoo.com/v2/collectConsent?sessionId={sessionId}',
            'data': data}
        get_args = base_args | {
            'url': f'https://guce.yahoo.com/copyConsent?sessionId={sessionId}',
            'data': data}
        if self._session_is_caching:
            post_args['expire_after'] = self._expire_after
            get_args['expire_after'] = self._expire_after
            self._session.post(**post_args)
            self._session.get(**get_args)
        else:
            self._session.post(**post_args)
            self._session.get(**get_args)
        utils.cookie = True
        self._save_session_cookies()
        return True

    @utils.log_indent_decorator
    def _get_crumb_csrf(self, proxy=None, timeout=30):
        # Credit goes to @bot-unit #1729

        if utils.reuse_crumb and utils.crumb is not None:
            utils.get_yf_logger().debug('reusing crumb')
            return utils.crumb

        if not self._get_cookie_csrf(proxy, timeout):
            # This cookie stored in session
            return None

        get_args = {
            'url': 'https://query2.finance.yahoo.com/v1/test/getcrumb', 
            'headers': self.user_agent_headers,
            'proxies': proxy,
            'timeout': timeout}
        if self._session_is_caching:
            get_args['expire_after'] = self._expire_after
            r = self._session.get(**get_args)
        else:
            r = self._session.get(**get_args)
        utils.crumb = r.text

        if utils.crumb is None or '<html>' in utils.crumb or utils.crumb == '':
            return None

        utils.get_yf_logger().debug(f"crumb = '{utils.crumb}'")
        return utils.crumb

    @utils.log_indent_decorator
    def _get_cookie_and_crumb(self, proxy=None, timeout=30):
        cookie, crumb = None, None

        utils.get_yf_logger().debug(f"cookie_mode = '{self._cookie_strategy}'")

        if self._cookie_strategy == 'csrf':
            crumb = self._get_crumb_csrf()
            if crumb is None:
                # Fail
                self._toggle_cookie_strategy()
                cookie, crumb = self._get_cookie_and_crumb_basic(proxy, timeout)
        else:
            # Fallback strategy
            cookie, crumb = self._get_cookie_and_crumb_basic(proxy, timeout)
            if cookie is None or crumb is None:
                # Fail
                self._toggle_cookie_strategy()
                crumb = self._get_crumb_csrf()

        return cookie, crumb

    @utils.log_indent_decorator
    def get(self, url, user_agent_headers=None, params=None, proxy=None, timeout=30):
        # Important: treat input arguments as immutable.

        if len(url) > 200:
            utils.get_yf_logger().debug(f'url={url[:200]}...')
        else:
            utils.get_yf_logger().debug(f'url={url}')
        utils.get_yf_logger().debug(f'params={params}')
        proxy = self._get_proxy(proxy)

        if params is None:
            params = {}
        if 'crumb' in params:
            raise Exception("Don't manually add 'crumb' to params dict, let data.py handle it")

        # Be careful which URLs get a crumb, because crumb breaks some fetches
        cookie, crumb = None, None
        # need_crumb = 'finance/quoteSummary' in url
        need_crumb = True
        if need_crumb:
            cookie, crumb = self._get_cookie_and_crumb()
        if crumb is not None:
            crumbs = {'crumb': crumb}
        else:
            crumbs = {}
        if self._cookie_strategy == 'basic' and cookie is not None:
            # Basic cookie strategy adds cookie to GET parameters
            cookies = {cookie.name: cookie.value}
        else:
            cookies = None

        request_args = {
            'url': url,
            'params': params | crumbs,
            'cookies': cookies,
            'proxies': proxy,
            'timeout': timeout,
            'headers': user_agent_headers or self.user_agent_headers
        }
        response = self._session.get(**request_args)

        if response.status_code >= 400 and need_crumb:
            # Retry with other cookie strategy
            self._toggle_cookie_strategy()
            cookie, crumb = self._get_cookie_and_crumb(proxy, timeout)
            request_args['params']['crumb'] = crumb
            if self._cookie_strategy == 'basic':
                request_args['cookies'] = {cookie.name: cookie.value}
            response = self._session.get(**request_args)

        return response

    @lru_cache_freezeargs
    @lru_cache(maxsize=cache_maxsize)
    def cache_get(self, url, user_agent_headers=None, params=None, proxy=None, timeout=30):
        return self.get(url, user_agent_headers, params, proxy, timeout)

    def _get_proxy(self, proxy):
        # setup proxy in requests format
        if proxy is not None:
            if isinstance(proxy, (dict, frozendict)) and "https" in proxy:
                proxy = proxy["https"]
            proxy = {"https": proxy}
        return proxy

    def get_raw_json(self, url, user_agent_headers=None, params=None, proxy=None, timeout=30):
        utils.get_yf_logger().debug(f'get_raw_json(): {url}')
        response = self.get(url, user_agent_headers=user_agent_headers, params=params, proxy=proxy, timeout=timeout)
        response.raise_for_status()
        return response.json()
