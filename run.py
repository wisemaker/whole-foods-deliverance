import argparse
import logging
import pickle
import os
from time import sleep
from random import uniform
from datetime import datetime
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
import chromedriver_binary

from config import Config
from notify import send_sms, send_telegram, alert, annoy

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def store_session_data(driver, path=Config.PKL_PATH):
    data = {
        'cookies': driver.get_cookies(),
        'storage': {
            k: driver.execute_script(
                'for(var k,s=window.{}Storage,d={{}},i=0;i<s.length;++i)'
                'd[k=s.key(i)]=s.getItem(k);return d'.format(k)
            ) for k in ['local', 'session']
        }
    }
    if any(data.values()):
        log.info('Writing session data to: ' + path)
        with open(path, 'wb') as file:
            pickle.dump(data, file)
    else:
        log.warning('No session data found')


def load_session_data(driver, path=Config.PKL_PATH):
    log.info('Reading session data from: ' + path)
    with open(path, 'rb') as file:
        data = pickle.load(file)
    if data.get('cookies'):
        log.info('Loading {} cookie values'.format(len(data['cookies'])))
        for c in data['cookies']:
            if c.get('expiry'):
                c['expiry'] = int(c['expiry'])
            driver.add_cookie(c)
    for _type, values in data['storage'].items():
        if values:
            log.info('Loading {} {}Storage values'.format(len(values), _type))
        for k, v in values.items():
            driver.execute_script(
                'window.{}Storage.setItem(arguments[0], arguments[1]);'.format(
                    _type
                ),
                k, v
            )


def wait_for_element(driver, locator, timeout=5):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(locator)
        )
    except TimeoutException:
        log.error("Timed out waiting for target element: {}".format(locator))
        raise


def remove_qs(url):
    """Remove URL query string the lazy way"""
    return url.split('?')[0]


def jitter(seconds, pct=20):
    """This seems unnecessary"""
    sleep(uniform(seconds*(1-pct/100), seconds*(1+pct/100)))


def get_element(driver, locator, **kwargs):
    wait_for_element(driver, locator, **kwargs)
    return driver.find_element(*locator)


def navigate(driver, locator, **kwargs):
    log.info("Navigating via locator: {}".format(locator))
    elem = get_element(driver, locator, **kwargs)
    jitter(.8)
    elem.click()


def is_logged_in(driver):
    if remove_qs(driver.current_url) == Config.BASE_URL:
        try:
            text = get_element(driver, Config.Locators.LOGIN).text
            return Config.Patterns.NOT_LOGGED_IN not in text
        except Exception:
            return False
    elif remove_qs(driver.current_url) == Config.AUTH_URL:
        return False
    else:
        # Lazily assume true if we are anywhere but BASE_URL and AUTH_URL
        return True


def wait_for_auth(driver, timeout_mins=10):
    t = datetime.now()
    alerted = []
    if is_logged_in(driver):
        log.debug('Already logged in')
        return
    log.info('Waiting for user login...')
    while not is_logged_in(driver):
        elapsed = int((datetime.now() - t).total_seconds() / 60)
        if is_logged_in(driver):
            log.info('Logged in')
            store_session_data(driver)
            break
        elif elapsed > timeout_mins:
            raise RuntimeError(
                'Timed out waiting for login (>= {}min)'.format(timeout_mins)
            )
        elif elapsed not in alerted:
            alerted.append(elapsed)
            alert('Log in to continue')
        sleep(1)


def slots_available(driver):
    slots = get_element(driver, Config.Locators.SLOTS)
    return Config.Patterns.NO_SLOTS not in slots.text


def navigate_to_slot_select(driver):
    log.info('Navigating to delivery slot selection')
    if remove_qs(driver.current_url) != Config.BASE_URL:
        log.info('Going home first')
        driver.get(Config.BASE_URL)
    navigate(driver, (By.ID, 'nav-cart'))
    navigate(driver, (
        By.XPATH,
        "//*[contains(text(),'{}')]/..".format(Config.Patterns.WF_CHECKOUT)
    ))
    navigate(driver, (
        By.XPATH,
        "//span[contains(@class, 'byg-continue-button')]"
    ))
    navigate(driver, (By.ID, 'subsContinueButton'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="wf-deliverance")
    parser.add_argument('--force_login', '-f', action='store_true',
                        help="Login and refresh session cookie if it exists")
    args = parser.parse_args()

    log.info('Invoking Selenium Chrome webdriver')
    driver = webdriver.Chrome()
    log.info('Navigating to ' + Config.BASE_URL)
    driver.get(Config.BASE_URL)

    if args.force_login or not os.path.exists(Config.PKL_PATH):
        # Login and capture Amazon session data...
        wait_for_auth(driver)
    else:
        # ...or load from storage
        load_session_data(driver)
        driver.refresh()
        if is_logged_in(driver):
            log.info('Successfully logged in via stored cookie')
        else:
            log.error('Error logging in with stored cookie')
            wait_for_auth(driver)
    try:
        navigate_to_slot_select(driver)
    except TimeoutException:
        if remove_qs(driver.current_url) in [Config.BASE_URL, Config.AUTH_URL]:
            wait_for_auth(driver)
            navigate_to_slot_select(driver)
        else:
            log.error('Navigation failed')
            raise
    # Check for delivery slots
    if slots_available(driver):
        annoy()
        alert('Delivery slots available. What do you need me for?', 'Sosumi')
    while not slots_available(driver):
        log.info('No slots found :( waiting...')
        jitter(25)
        driver.refresh()
        if slots_available(driver):
            alert('Delivery slots found')
            send_sms(get_element(driver, Config.Locators.SLOTS).text)
            send_telegram(get_element(driver, Config.Locators.SLOTS).text)
            break
    try:
        # Allow time to check out manually
        sleep(900)
    except KeyboardInterrupt:
        log.warning('Slumber disturbed')
    log.info('Closing webdriver')
    driver.close()
