from datetime import datetime, timedelta
from odoo import fields, models
from .payment import ENTITIES


class SaleOrder(models.Model):
    """
    Generate the MB payment reference for the Sales Order,
    on Sales Order CONFIRM
    """
    _inherit = "sale.order"

    # TODO: add these fields to the Sales Order
    # TODO: add tehse fields to the SAles Order Report
    ep_entity = fields.Selection(ENTITIES, string="ep_entity", readonly=True)
    ep_reference = fields.Char(string="ep_reference", readonly=True)
    ep_value = fields.Float(string="ep_value", readonly=True)

    def action_confirm(self):
        easypay = self.env.ref("payment_easypay.payment_acquirer_easypay")
        if easypay:
            for order in self.filtered("payment_term_id.use_easypay_reference"):
                max_date = False
                if easypay.ep_entity == "11683":
                    max_date = field.Date.today() + timedelta(
                        days=easypay.ep_expiration_days
                    )
                ref = easypay.request_payment_identifier(order, max_date)
                if ref:
                    vals = {
                        "ep_entity": ref["ep_entity"],
                        "ep_reference": ref["ep_reference"],
                        "ep_value": ref["ep_value"],
                    }
                    order.write(vals)
        return super().action_confirm()

class PaymentTerm(models.Model):
    _inherit = "account.payment.term"

    # TODO: add this field to the Payment
    use_easypay_reference = fields.Boolean(
        string="Use Easypay Reference",
        help="Generate an Easypay payment reference when confirming the Sales Order",
    )
