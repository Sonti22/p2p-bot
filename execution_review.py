"""Explicit eligibility review for a public P2P offer, without inventing acceptance."""
import hashlib
import math

from bankmodel import Blocked


def key(ad):
    return ':'.join((ad.ex, ad.side, ad.ad_id or '', ad.asset))


def terms_hash(ad):
    return hashlib.sha256((ad.terms or '').encode('utf-8')).hexdigest()


def check(profile, ad, now):
    import bankmodel
    if bankmodel.SCENARIO.get() is not None:
        from scenarios import review_offer
        return review_offer(profile, ad, now)
    if not ad.ad_id:
        raise Blocked('Нет устойчивого идентификатора P2P-объявления')
    review = profile.get('offer_reviews', {}).get(key(ad))
    if not isinstance(review, dict):
        raise Blocked('Не проверены требования конкретного P2P-объявления')
    if review.get('terms_hash') != terms_hash(ad):
        raise Blocked('Условия P2P-объявления изменились')
    for field in ('eligible', 'identity_match', 'no_third_party', 'p2p_fee_confirmed'):
        if review.get(field) is not True:
            raise Blocked('Не подтверждено условие объявления: ' + field)
    try:
        checked, expires = float(review.get('checked_at', 0)), float(review.get('valid_until', 0))
    except (ValueError, TypeError) as exc:
        raise Blocked('Некорректная дата проверки объявления') from exc
    if not all(math.isfinite(x) for x in (checked, expires)) or not 0 < checked <= now <= expires:
        raise Blocked('Проверка P2P-объявления устарела')
    if review.get('p2p_fee') != '0':
        raise Blocked('Ненулевая P2P-комиссия требует подтверждённой модели валюты списания')
    seconds = review.get('payment_seconds')
    release = review.get('release_seconds')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 86400
           for x in (seconds, release)):
        raise Blocked('Не подтверждено время оплаты/разблокировки P2P')
    return review
