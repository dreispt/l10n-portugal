# coding: utf-8
import logging
import pprint
import requests
from datetime import datetime
from werkzeug import url_encode

from odoo import fields, models, api, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

ENTITIES = [('10611', '10611'), ('11683', '11683'), ('21098', '21098')]


class PaymentAcquirer(models.Model):
    _inherit = 'payment.acquirer'

    provider = fields.Selection(selection_add=[('easypay', 'Easypay')])
    ep_cin = fields.Char(string='CIN', help='The CIN (Client Information Number) of your easypay account.')
    ep_user = fields.Char(string='User', help='The USER of your easypay account.')
    ep_entity = fields.Selection(ENTITIES, string='Entity',
                                 help='The ENTITY of your easypay account.', default='10611')
    ep_expiration_days = fields.Integer(string='Expire in Days',
                                        help='You must set this if you are using 11683 entity.')

    @api.multi
    def easypay_get_form_action_url(self):
        return '/payment/easypay'

    def _get_easypay_url(self, type):
        if self.environment == 'prod':
            return ('https://www.easypay.pt/_s/api_easypay_%s.php' % type)
        return ('http://test.easypay.pt/_s/api_easypay_%s.php' % type)

    @api.multi
    def write(self, vals):
        if vals.get('ep_expiration_days', False) and not 1 <= vals.get('ep_expiration_days') <= 365:
            raise ValidationError(_('Expire in days must be between 1 and 365.'))
        return super(PaymentAcquirer, self).write(vals)

    def request_payment_identifier(self, order, o_max_date):
        try:
            partner = order.partner_id

            easypay_url = self._get_easypay_url('01BG')
            easypay_url += '?ep_cin=%s' % self.ep_cin
            easypay_url += '&ep_user=%s' % self.ep_user
            easypay_url += '&ep_entity=%s' % self.ep_entity
            easypay_url += '&ep_ref_type=auto'
            easypay_url += '&ep_country=PT'
            easypay_url += '&ep_language=PT'
            easypay_url += '&t_value=%s' % order.amount_total
            easypay_url += '&t_key=%i' % order.id
            easypay_url += '&%s' % url_encode({'o_name': partner.name})
            easypay_url += '&%s' % url_encode({'o_description': order.name})
            easypay_url += '&o_obs='
            easypay_url += '&%s' % url_encode({'o_mobile': partner.mobile or ''})
            easypay_url += '&%s' % url_encode({'o_email': partner.email or ''})
            if o_max_date:
                easypay_url += '&o_max_date=%s' % (o_max_date)
            easypay_url += '&ep_partner=%s' % self.ep_user

            _logger.warning('Easypay request payment identifier %s' % easypay_url)

            r = requests.get(easypay_url)
            root = ET.fromstring(r.content)

            response = {
                'ep_status': None,
                'ep_message': None,
                'ep_cin': None,
                'ep_user': None,
                'ep_entity': None,
                'ep_reference': None,
                'ep_value': None,
                't_key': None,
                'ep_link': None,
                'ep_boleto': None,
                'ep_currency': None,
                'ep_original_value': None,
            }

            for elem in root.iter(tag='getautoMB'):
                for key, value in response.items():
                    element = elem.find(key)
                    if element != None:
                        response[key] = element.text

            _logger.warning('Easypay response %s' % pprint.pformat(response))

            if response['ep_status'] == 'ok0':
                return response

            return False
        except Exception as e:
            _logger.warning('Easypay error processing request: %s', str(e))

    def fetch_payment_detail(self, ep_doc, ep_key):
        try:
            easypay_url = self._get_easypay_url('03AG')
            easypay_url += '?ep_cin=%s' % self.ep_cin
            easypay_url += '&ep_user=%s' % self.ep_user
            easypay_url += '&ep_doc=%s' % ep_doc
            easypay_url += '&ep_key=%s' % ep_key

            _logger.warning('Easypay fetch payment detail %s' % easypay_url)

            r = requests.get(easypay_url)
            root = ET.fromstring(r.content)

            response = {
                'ep_key': None,
                'ep_doc': None,
                'ep_cin': None,
                'ep_user': None,
                'ep_status': None,
                'ep_entity': None,
                'ep_reference': None,
                'ep_value': None,
                'ep_date': None,
                'ep_payment_type': None,
                'ep_value_fixed': None,
                'ep_value_var': None,
                'ep_value_tax': None,
                'ep_value_transf': None,
                'ep_date_transf': None,
                't_key': None,
                'ep_type': None,
                'ep_message': None,
                'ep_date_read': None,
                'ep_status_read': None,
                'o_key': None,
                'o_obs': None,
                'o_email': None,
                'o_mobile': None,
            }

            for elem in root.iter(tag='getautoMB_detail'):
                for key, value in response.items():
                    element = elem.find(key)
                    if element != None:
                        response[key] = element.text

            _logger.warning('Easypay response %s' % pprint.pformat(response))

            if response['ep_status'] == 'ok0':
                return response

            return False
        except Exception as e:
            _logger.warning('Easypay error processing request: %s', str(e))


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    @api.multi
    @api.depends('ep_reference')
    def _compute_display_ep_reference(self):
        for rec in self:
            if rec.ep_reference:
                if len(rec.ep_reference) > 3:
                    ep_reference = ''.join(rec.ep_reference.split())
                    ep_reference = [ep_reference[i:i + 3] for i in range(0, len(ep_reference), 3)]
                    rec.display_ep_reference = ' '.join(x for x in ep_reference)
                else:
                    rec.display_ep_reference = rec.ep_reference
            else:
                rec.display_ep_reference = None

    @api.multi
    @api.depends('o_max_date')
    def _compute_display_o_max_date(self):
        for rec in self:
            if rec.o_max_date:
                rec.display_o_max_date = datetime.strptime(rec.o_max_date, '%Y-%m-%d').strftime('%d-%m-%Y')
            else:
                rec.display_o_max_date = None

    @api.multi
    @api.depends('amount')
    def _compute_display_amount(self):
        for rec in self:
            rec.display_amount = self.format_currency(rec.amount)

    @api.multi
    @api.depends('acquirer_id')
    def _compute_is_easypay(self):
        easypay_id = self.env.ref('payment_easypay.payment_acquirer_easypay', None)
        for rec in self:
            rec.is_easypay = bool(easypay_id and easypay_id.id == rec.acquirer_id.id)

    ep_status = fields.Char(string='ep_status', default='pending', readonly=True)
    ep_cin = fields.Char(string='ep_cin', readonly=True)
    ep_user = fields.Char(string='ep_user', readonly=True)
    ep_entity = fields.Selection(ENTITIES, string='ep_entity', readonly=True)
    ep_reference = fields.Char(string='ep_reference', readonly=True)
    ep_value = fields.Float(string='ep_value', readonly=True)
    t_key = fields.Char(string='t_key', readonly=True)
    ep_boleto = fields.Char(string='ep_boleto', readonly=True)
    ep_currency = fields.Char(string='ep_currency', readonly=True)
    ep_original_value = fields.Float(string='ep_original_value', readonly=True)
    o_max_date = fields.Date(string='o_max_date', readonly=True)
    display_o_max_date = fields.Char(compute='_compute_display_o_max_date', store=True, readonly=True)
    display_ep_reference = fields.Char(compute='_compute_display_ep_reference', store=True, readonly=True)
    display_amount = fields.Char(compute='_compute_display_amount', store=True, readonly=True)
    metadata = fields.Text(readonly=True)
    easypay_notification_ids = fields.One2many(comodel_name='payment.transaction.easypay', inverse_name='payment_id',
                                               readonly=True)
    is_easypay = fields.Boolean(compute='_compute_is_easypay', store=True, readonly=True)
    notification_metadata = fields.Text(related='easypay_notification_ids.metadata', readonly=True)

    def format_currency(self, price):
        if price.is_integer():
            return '{:,.0f} €'.format(price).replace(',', '-').replace('.', ',').replace('-', '.')
        return '{:,.2f} €'.format(price).replace(',', '-').replace('.', ',').replace('-', '.')

    def update_easypay_transaction(self, values, o_max_date):
        values.update({
            'metadata': pprint.pformat(values),
            'state_message': values.get('ep_message'),
            'state': 'pending',
            'o_max_date': values['ep_entity'] == '11683' and o_max_date or None,
        })

        values.pop('ep_message', None)
        values.pop('ep_link', None)

        self.sudo().write(values)


class PaymentTransactionEasypay(models.Model):
    _name = 'payment.transaction.easypay'
    _description = 'Easypay Notification'

    payment_id = fields.Many2one(string='Payment Transaction', comodel_name='payment.transaction', readonly=True)
    ep_key = fields.Integer(string='ep_key', related='id', store=True, readonly=True)
    ep_doc = fields.Char(string='ep_doc', help='Document number of the payment received.', readonly=True)
    ep_cin = fields.Char(string='ep_cin', help='CIN of the payment received.', readonly=True)
    ep_user = fields.Char(string='ep_user', help='USER of the payment received.', readonly=True)
    ep_status = fields.Char(string='ep_status', default='pending', readonly=True)
    ep_entity = fields.Selection(ENTITIES, string='ep_entity', readonly=True)
    ep_reference = fields.Char(string='ep_reference', readonly=True)
    ep_value = fields.Float(string='ep_value', readonly=True)
    ep_date = fields.Datetime(string='ep_date', readonly=True)
    ep_payment_type = fields.Char(string='ep_payment_type', readonly=True)
    ep_value_fixed = fields.Float(string='ep_value_fixed', readonly=True)
    ep_value_var = fields.Float(string='ep_value_var', readonly=True)
    ep_value_tax = fields.Float(string='ep_value_tax', readonly=True)
    ep_value_transf = fields.Float(string='ep_value_transf', readonly=True)
    ep_date_transf = fields.Date(string='ep_date_transf', readonly=True)
    t_key = fields.Char(string='t_key', readonly=True)
    ep_type = fields.Char(string='ep_type', help='Acquirer used for this transaction', readonly=True)
    metadata = fields.Text(string='Metadata', readonly=True)

    _sql_constraints = [
        ('ep_doc_uniq', 'unique(ep_doc)', 'The document number must be unique.'),
    ]
