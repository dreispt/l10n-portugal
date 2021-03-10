# -*- coding: utf-8 -*-
import logging
import pprint
from datetime import datetime, timedelta

from cerberus import Validator
from odoo import http, _
from odoo.http import request
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)


class PaymentEasypay(http.Controller):
    @http.route(['/payment/easypay'], type='http', auth='public', website=True, sitemap=False)
    def payment_easypay(self, **kw):
        env = request.env

        sale_order_id = request.session.get('sale_order_id')
        if sale_order_id:
            order = env['sale.order'].sudo().browse(sale_order_id).exists()
            acquirer = env.ref('payment_easypay.payment_acquirer_easypay').exists()

            if order and acquirer:
                if order.state in ['draft', 'sent']:
                    order.with_context(send_email=True).action_confirm()

                # Check if a payment transaction exists for this order and acquirer before create a new one
                tx = env['payment.transaction'].sudo().search([
                    ('sale_order_id', '=', order.id),
                    ('acquirer_id', '=', acquirer.id),
                ], limit=1)

                if tx and tx.state in ['draft', 'pending']:
                    if tx.state == 'draft':
                        o_max_date = False
                        if acquirer.ep_entity == '11683':
                            o_max_date = datetime.now().date() + timedelta(days=acquirer.ep_expiration_days)

                        r = acquirer.request_payment_identifier(order, o_max_date)
                        if not r:
                            return http.request.render('payment_easypay.payment_form')

                        tx.update_easypay_transaction(r, o_max_date)

                    return http.request.render('payment_easypay.payment_form', {'tx': tx})

        return request.redirect('/shop')

    @http.route(['/payment/easypay/notification'], type='http', auth='public', methods=['GET'])
    def payment_easypay_notification(self, **kw):
        env = request.env

        schema = {
            'ep_cin': {
                'type': 'string',
                'required': True,
                'empty': False,
            },
            'ep_doc': {
                'type': 'string',
                'required': True,
                'empty': False,
            },
            'ep_user': {
                'type': 'string',
                'required': True,
                'empty': False,
            },
            'ep_type': {
                'type': 'string',
                'required': False,
                'empty': True,
            },
        }

        v = Validator(schema, purge_unknown=True)
        v.validate(kw)

        if not v.errors:
            n = env['payment.transaction.easypay'].sudo().search([('ep_doc', '=', kw['ep_doc'])], limit=1)
            if not n:
                n = env['payment.transaction.easypay'].sudo().create({
                    'ep_cin': kw['ep_cin'],
                    'ep_doc': kw['ep_doc'],
                    'ep_user': kw['ep_user'],
                    'ep_type': kw.get('ep_type', None),
                })

            if n.ep_status == 'pending':
                acquirer = env.ref('payment_easypay.payment_acquirer_easypay')

                r = acquirer.fetch_payment_detail(kw['ep_doc'], n.ep_key)
                if r:
                    r['metadata'] = pprint.pformat(r)

                    r.pop('ep_message', None)
                    r.pop('ep_date_read', None)
                    r.pop('ep_status_read', None)
                    r.pop('o_key', None)
                    r.pop('o_obs', None)
                    r.pop('o_email', None)
                    r.pop('o_mobile', None)

                    n.sudo().write(r)

                    tx = env['payment.transaction'].sudo().search([('sale_order_id', '=', int(n.t_key))], limit=1)
                    if tx and tx.state == 'pending':
                        n.sudo().write({
                            'payment_id': tx.id,
                        })

                        tx.sudo().write({
                            'state': 'done',
                            'date_validate': datetime.now(),
                        })

                        # Verify SO/TX match, excluding tx.fees which are currently not included in SO
                        amount_matches = float_compare(tx.amount, tx.sale_order_id.amount_total, 2) == 0
                        acquirer_name = tx.acquirer_id.provider or 'unknown'

                        if amount_matches:
                            _logger.warning('<%s> transaction completed, auto-confirming order %s (ID %s)' % (
                                acquirer_name, tx.sale_order_id.name, tx.sale_order_id.id))
                            tx._generate_and_pay_invoice()
                        else:
                            _logger.warning(
                                '<%s> transaction AMOUNT MISMATCH for order %s (ID %s): expected %r, got %r' % (
                                    acquirer_name, tx.sale_order_id.name, tx.sale_order_id.id,
                                    tx.sale_order_id.amount_total,
                                    tx.amount))
                            tx.sale_order_id.message_post(
                                subject=_("Amount Mismatch (%s)") % acquirer_name,
                                body=_(
                                    "The sale order was not confirmed despite response from the acquirer (%s): SO amount is %r but acquirer replied with %r.") % (
                                         acquirer_name,
                                         tx.sale_order_id.amount_total,
                                         tx.amount,
                                     )
                            )

            values = {
                'ep_cin': kw['ep_cin'],
                'ep_user': kw['ep_user'],
                'ep_doc': kw['ep_doc'],
                'ep_key': n.ep_key,
            }

            return request.render('payment_easypay.easypay_notification', values, mimetype='text/plain')

        return ''
