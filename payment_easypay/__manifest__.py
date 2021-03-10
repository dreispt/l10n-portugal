# -*- coding: utf-8 -*-
{
    'name': 'Easypay Payment Acquirer',
    'category': 'Accounting',
    'summary': 'Payment Acquirer: Easypay Implementation',
    'version': '11.0.1.0',
    'description': """
Easypay Payment Acquirer
========================
A Easypay é uma instituição de pagamentos eletrónicos que reúne diferentes opções de pagamento numa única plataforma.
Através deste sistema de pagamentos eletrónicos é possível pagar por Referência Multibanco, Cartão de Crédito,
Débito Direto SEPA (Single Euro Payments Area) ou Boleto Bancário.

10611 - Referências Checkdigit:
- Tipologia de referências bloqueadas ao montante, sem data de validade para pagamento.

11683 - Referências com Validade:
- Tipologia de referências com data de validade, estipulada por nós. Estas referências são bloqueadas à data e ao montante;
- Expira automaticamente após término da data.

21098 - Referências por Ficheiro:
- Tipologia de referências com valor aberto (pode variar). Funcionam como uma conta-corrente (referência é sempre a mesma, o valor pode mudar);
- Frequentemente utilizada para pagamento de mensalidades, quotas, etc.

""",
    'author': 'OdooGap',
    'website': 'https://www.odoogap.com',
    'depends': ['website_sale', 'account_invoicing'],
    'data': [
        'data/payment_acquirer_data.xml',
        'security/ir.model.access.csv',
        'views/payment_views.xml',
        'views/payment_templates.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'post_init_hook': 'create_missing_journal_for_acquirers',
}
