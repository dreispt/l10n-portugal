# Copyright (C) 2026 Daniel Reis
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

import xml.etree.ElementTree as ET

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase
from odoo.tools import format_amount

CSV_HEADER = (
    "date_start,date_end,region,table_code,lower_bound,upper_bound,"
    "marginal_rate,parcela_abater,dependent_parcela,"
    "forces_disabled_parcela,effective_rate_limit,ias,"
    "disabled_dep_parcela_single,disabled_dep_parcela_joint,"
    "spouse_disabled_parcela,"
    "pension_dep_parcela_single,pension_dep_parcela_joint,"
    "pension_dep_parcela_single_income\n"
)
# Set-level Despacho constants used by the test tables:
# IAS, disabled-dep single/joint, spouse-disabled, pension-dep
# single/joint/single-income.
SET_VALUES = "522.50,50,25,0,0,0,0"


class TestL10nPtPayroll(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.structure = cls.env.ref("l10n_pt_payroll.structure_pt")
        cls.employee = cls.env["hr.employee"].create(
            {
                "name": "Test Employee",
                "company_id": cls.env.company.id,
            }
        )
        # Initialize the contract (hr.version) created at employee creation
        cls.employee.version_id.write(
            {
                "contract_date_start": "2026-01-01",
                "contract_date_end": False,
                "wage": 1400.0,
                "struct_id": cls.structure.id,
                "resource_calendar_id": cls.env.company.resource_calendar_id.id,
                # IRS determinants: single, no dependents -> table I
                "l10n_pt_irs_family_status": "single",
                "l10n_pt_irs_region": "continente",
            }
        )
        cls.env["hr.pt.irs.withholding.import"]._import_csv_data(cls._test_csv())

    def setUp(self):
        super().setUp()
        # Tests share the class employee: reset the mutable IRS
        # determinants to the table-I baseline before each test.
        self.employee.write(
            {
                "l10n_pt_irs_dependents": 0,
                "l10n_pt_irs_disabled_dependents": 0,
                "l10n_pt_irs_disabled_dep_factor": 1,
                "l10n_pt_irs_spouse_disabled": False,
                "l10n_pt_military_disabled": False,
                "l10n_pt_irs_non_resident": False,
                "l10n_pt_irs_single_payer_declared": False,
                "l10n_pt_irs_preferred_rate": 0.0,
                "l10n_pt_irs_jovem": False,
                "l10n_pt_irs_income_years": 0,
            }
        )

    @staticmethod
    def _test_csv():
        """A minimal synthetic table set: two brackets on variants I,
        II and III, continente, covering 2026. Variant II is needed
        because dependents switch the derived table variant."""
        return CSV_HEADER + "".join(
            f"2026-01-01,2026-12-31,continente,{code},{lower},{upper},"
            f"{rate},{parcela},30,0,,{SET_VALUES}\n"
            for code in ("I", "II", "III")
            for lower, upper, rate, parcela in (
                ("0", "2000", "20", "0"),
                ("2000", "", "40", "200"),
            )
        )

    def _make_payslip(self):
        payslip = self.env["hr.payslip"].create(
            {
                "employee_id": self.employee.id,
                "date_from": "2026-06-01",
                "date_to": "2026-06-30",
            }
        )
        payslip.onchange_employee()
        return payslip

    def test_pt_payroll_structure(self):
        self.assertEqual(self.structure.code, "PT")
        self.assertTrue(
            {
                "BASIC",
                "SA",
                "GROSS",
                "SS",
                "IRS",
                "SS_COMP",
                "SAT_COMP",
                "NET",
            }
            <= set(self.structure.rule_ids.mapped("code"))
        )

    def test_pt_payslip_compute(self):
        """A monthly payslip computes all PT lines, with the IRS
        retention resolved from the imported withholding table."""
        payslip = self._make_payslip()
        self.assertEqual(
            payslip.struct_id,
            self.structure,
            "The payslip uses the PT structure from the contract",
        )
        payslip.compute_sheet()

        lines = {line.code: line.total for line in payslip.line_ids}
        work100 = payslip.worked_days_line_ids.filtered(
            lambda line: line.code == "WORK100"
        )
        meal = 10.20 * work100.number_of_days
        # R = taxable BASIC only = 1400 -> first bracket: 20% of 1400
        self.assertAlmostEqual(lines["BASIC"], 1400.0)
        self.assertAlmostEqual(lines["SA"], meal)
        self.assertAlmostEqual(lines["GROSS"], 1400.0 + meal)
        self.assertAlmostEqual(lines["SS"], -0.11 * 1400.0)
        self.assertAlmostEqual(lines["IRS"], -280.0)
        self.assertAlmostEqual(lines["SS_COMP"], 0.2375 * 1400.0)
        self.assertAlmostEqual(lines["SAT_COMP"], 0.0)
        self.assertAlmostEqual(lines["NET"], 1400.0 + meal - 0.11 * 1400.0 - 280.0)

    def test_declaration_rule_defaults(self):
        """A payroll officer starts with known AT and SS mappings,
        not a guessed meal split."""
        rules = {rule.code: rule for rule in self.structure.rule_ids}
        basic = rules["BASIC"]
        self.assertEqual(basic.l10n_pt_dmr_at_role, "income")
        self.assertEqual(basic.l10n_pt_dmr_code, "A")
        self.assertEqual(basic.l10n_pt_ss_nature_code, "P")
        self.assertEqual(rules["IRS"].l10n_pt_dmr_at_role, "withholding")
        self.assertEqual(rules["SS"].l10n_pt_dmr_at_role, "contribution")
        for code in ("GROSS", "NET", "SS_COMP", "SAT_COMP"):
            self.assertEqual(rules[code].l10n_pt_dmr_at_role, "ignore")
        self.assertFalse(rules["SA"].l10n_pt_dmr_at_role)
        self.assertFalse(rules["SA"].l10n_pt_ss_nature_code)

    def test_social_security_base_configured_per_earning(self):
        """A payroll officer independently configures IRS and SS on additional pay."""
        allowances = self.env.ref("l10n_pt_payroll.categ_pt_alw")
        base = self.env.ref("l10n_pt_payroll.categ_pt_basic")
        extra_rules = self.env["hr.salary.rule"].create(
            [
                {
                    "name": "Contributory Addition",
                    "code": "SS_ADD",
                    "category_id": allowances.id,
                    "sequence": 30,
                    "amount_select": "fix",
                    "amount_fix": 200.0,
                    "l10n_pt_ss_contribution_base": "include",
                },
                {
                    "name": "IRS-only Addition",
                    "code": "IRS_ADD",
                    "category_id": base.id,
                    "sequence": 31,
                    "amount_select": "fix",
                    "amount_fix": 100.0,
                    "l10n_pt_irs_taxable": 1.0,
                    "l10n_pt_ss_contribution_base": "exclude",
                },
            ]
        )
        self.structure.rule_ids = [Command.link(rule.id) for rule in extra_rules]
        payslip = self._make_payslip()
        payslip.compute_sheet()

        lines = {line.code: line for line in payslip.line_ids}
        self.assertAlmostEqual(lines["IRS"].total, -300.0)
        self.assertAlmostEqual(lines["SS"].total, -176.0)
        self.assertAlmostEqual(lines["SS_COMP"].total, 380.0)
        # The SS lines expose the signed contribution base in amount
        # and the regime rate in rate so reports show base x rate.
        self.assertAlmostEqual(lines["SS"].amount, -1600.0)
        self.assertAlmostEqual(lines["SS"].rate, 11.0)
        self.assertAlmostEqual(lines["SS_COMP"].amount, 1600.0)
        self.assertAlmostEqual(lines["SS_COMP"].rate, 23.75)
        self.assertIn("11.0000%", lines["SS"].name)
        self.assertIn("23.7500%", lines["SS_COMP"].name)
        # IRS keeps the booked retention as amount at rate 100 and
        # embeds the basis and applied rates in the line name.
        self.assertAlmostEqual(lines["IRS"].amount, -300.0)
        self.assertAlmostEqual(lines["IRS"].rate, 100.0)
        currency = self.env.company.currency_id
        self.assertIn(
            f"Taxable base: {format_amount(self.env, 1500.0, currency)}",
            lines["IRS"].name,
        )
        self.assertIn("applied rate: 20.000%", lines["IRS"].name)
        self.assertIn(
            f"formula deductions: {format_amount(self.env, 0.0, currency)}",
            lines["IRS"].name,
        )
        self.assertIn("monthly effective withholding rate: 20.00%", lines["IRS"].name)

    def test_social_security_fractional_base_precision_fallback(self):
        """A fractional contribution base that column rounding would push
        across a cent boundary falls back to the booked total in Amount
        at rate 100, with the base shown in the line name instead."""
        fractional = self.env["hr.salary.rule"].create(
            {
                "name": "Fractional Contributory Addition",
                "code": "SS_FRAC",
                "category_id": self.env.ref("l10n_pt_payroll.categ_pt_alw").id,
                "sequence": 32,
                "amount_select": "percentage",
                "amount_percentage": 10.029,
                "amount_percentage_base": "categories.BASIC",
                "l10n_pt_ss_contribution_base": "include",
            }
        )
        self.structure.rule_ids = [Command.link(fractional.id)]
        payslip = self._make_payslip()
        payslip.compute_sheet()

        lines = {line.code: line for line in payslip.line_ids}
        # Base = 1400 + 10.029% x 1400 = 1540.406; employee contribution
        # = -1540.406 x 11% = -169.44466 books as -169.44, while the
        # rounded Amount/Rate columns would recompute -169.45, so the
        # rule falls back to the total at rate 100.
        self.assertAlmostEqual(lines["SS"].total, -169.44)
        self.assertAlmostEqual(lines["SS"].amount, -169.44)
        self.assertAlmostEqual(lines["SS"].rate, 100.0)
        self.assertIn("11.0000%", lines["SS"].name)
        self.assertIn("Contribution base:", lines["SS"].name)
        # The employer rate does not cross a rounding boundary on the
        # same base: it keeps the native base/rate columns.
        self.assertAlmostEqual(lines["SS_COMP"].amount, 1540.41)
        self.assertAlmostEqual(lines["SS_COMP"].rate, 23.75)
        self.assertIn("23.7500%", lines["SS_COMP"].name)
        self.assertNotIn("Contribution base:", lines["SS_COMP"].name)

    def test_social_security_rates_from_regime(self):
        """The SS rates come from the employee's Social Security regime:
        changing the regime rates changes the payslip lines, and the SAT
        premium comes from the employee's work insurance policy."""
        regime = self.employee.l10n_pt_ss_regime_id
        regime.write(
            {
                "l10n_pt_employee_rate": 10.0,
                "l10n_pt_employer_rate": 20.0,
            }
        )
        insurer = self.env["res.partner"].create({"name": "Test Insurer"})
        policy = self.env["l10n.pt.work.insurance"].create(
            {
                "insurer_id": insurer.id,
                "policy_number": "SAT-001",
                "rate": 1.5,
            }
        )
        self.employee.l10n_pt_work_insurance_id = policy
        payslip = self._make_payslip()
        payslip.compute_sheet()

        lines = {line.code: line.total for line in payslip.line_ids}
        self.assertAlmostEqual(lines["SS"], -0.10 * 1400.0)
        self.assertAlmostEqual(lines["SS_COMP"], 0.20 * 1400.0)
        self.assertAlmostEqual(lines["SAT_COMP"], 0.015 * 1400.0)

    def test_ss_regime_seed_data(self):
        """The bundled regime records carry the official situacao
        contributiva rates and DR rate codes."""
        cases = {
            "l10n_pt_ss_regime_geral": (11.0, 23.75, "000"),
            "l10n_pt_ss_regime_pre_reforma": (11.0, 23.75, "123"),
            "l10n_pt_ss_regime_moe_gerencia": (11.0, 23.75, "142"),
            "l10n_pt_ss_regime_moe_geral": (9.3, 20.3, "669"),
            "l10n_pt_ss_regime_moe_ipss": (9.9, 22.3, "183"),
        }
        for xmlid, (employee, employer, dr_code) in cases.items():
            regime = self.env.ref(f"l10n_pt_hr.{xmlid}")
            with self.subTest(regime=regime.code):
                self.assertEqual(regime.l10n_pt_employee_rate, employee)
                self.assertEqual(regime.l10n_pt_employer_rate, employer)
                self.assertEqual(regime.l10n_pt_dr_code, dr_code)
                self.assertAlmostEqual(regime.l10n_pt_total_rate, employee + employer)

    def test_irs_retention_preferred_rate(self):
        """A preferred rate replaces only the marginal rate (Despacho
        n.5-e): the table deductions still apply."""
        self.employee.write(
            {
                "l10n_pt_irs_preferred_rate": 25.0,
                "l10n_pt_irs_dependents": 2,
            }
        )
        payslip = self._make_payslip()
        payslip.compute_sheet()
        lines = {line.code: line.total for line in payslip.line_ids}
        # 1400 * 25% - 30 * 2 = 350 - 60 = 290
        self.assertAlmostEqual(lines["IRS"], -290.0)

    def test_irs_retention_non_resident(self):
        """A non-resident is withheld a flat 25% without any table
        deduction."""
        self.employee.l10n_pt_irs_non_resident = True
        payslip = self._make_payslip()
        payslip.compute_sheet()
        lines = {line.code: line.total for line in payslip.line_ids}
        self.assertAlmostEqual(lines["IRS"], -0.25 * 1400.0)
        irs_line = payslip.line_ids.filtered(lambda line: line.code == "IRS")
        # The flat 25% rate and the unadjusted base appear in the name,
        # unlike the progressive table path.
        self.assertIn("applied rate: 25.000%", irs_line.name)
        self.assertIn(
            f"Taxable base: "
            f"{format_amount(self.env, 1400.0, self.env.company.currency_id)}",
            irs_line.name,
        )
        self.assertIn("monthly effective withholding rate: 25.00%", irs_line.name)
        self.assertNotIn("income before relief", irs_line.name)

    def test_irs_retention_dependents(self):
        """Dependents reduce the retention through the per-dependent
        parcela; three or more also drop the marginal rate by 1 p.p."""
        self.employee.write(
            {
                "l10n_pt_irs_dependents": 2,
                "l10n_pt_irs_disabled_dependents": 1,
            }
        )
        payslip = self._make_payslip()
        payslip.compute_sheet()
        lines = {line.code: line.total for line in payslip.line_ids}
        # 1400 * 20% - 30 * 2 - 50 * 1 = 280 - 110 = 170
        self.assertAlmostEqual(lines["IRS"], -170.0)
        # Three or more dependents: marginal rate 20% -> 19%
        self.employee.l10n_pt_irs_dependents = 3
        payslip = self._make_payslip()
        payslip.compute_sheet()
        lines = {line.code: line.total for line in payslip.line_ids}
        # 1400 * 19% - 30 * 3 - 50 * 1 = 266 - 140 = 126
        self.assertAlmostEqual(lines["IRS"], -126.0)

    def test_irs_retention_disabled_dep_factor(self):
        """Despacho n.6: the employee may multiply the per-disabled-
        dependent deduction — up to 3x for a single worker."""
        self.employee.write(
            {
                "l10n_pt_irs_family_status": "single",
                "l10n_pt_irs_disabled_dependents": 1,
                "l10n_pt_irs_disabled_dep_factor": 3,
            }
        )
        payslip = self._make_payslip()
        payslip.compute_sheet()
        lines = {line.code: line.total for line in payslip.line_ids}
        # 1400 * 20% - 50 * 1 * 3 = 280 - 150 = 130
        self.assertAlmostEqual(lines["IRS"], -130.0)

    def test_irs_retention_disabled_spouse(self):
        """Despacho n.5-b: a married worker filing alone gets an extra
        deduction when the spouse is disabled and has no A/H income."""
        self.employee.write(
            {
                "l10n_pt_irs_family_status": "married_single_income",
                "l10n_pt_irs_spouse_disabled": True,
            }
        )
        variant_iii = self.env.ref("l10n_pt_hr.l10n_pt_irs_table_3")
        table = self.env["hr.pt.irs.withholding.table"].search(
            [
                ("region", "=", "continente"),
                ("table_id", "=", variant_iii.id),
            ],
            limit=1,
        )
        table.spouse_disabled_parcela = 100.0
        payslip = self._make_payslip()
        payslip.compute_sheet()
        lines = {line.code: line.total for line in payslip.line_ids}
        # table III bracket: 1400 * 20% - 100 = 180
        self.assertAlmostEqual(lines["IRS"], -180.0)

    def test_irs_retention_jovem(self):
        """IRS Jovem withholds on the non-exempt portion: income year 5
        exempts 50% of the income up to 55 x IAS, taxed at the effective
        rate of the full income per AT Circular 20274/2025."""
        # June-only overlay: the exemption base is taxed through the
        # effective rate, not a second marginal bracket.
        self.env["hr.pt.irs.withholding.import"]._import_csv_data(
            CSV_HEADER
            + "2026-06-01,2026-06-30,continente,I,0,920,0,0,0,0,,"
            + SET_VALUES
            + "\n"
            + "2026-06-01,2026-06-30,continente,I,920,,24.1,193.33,0,0,,"
            + SET_VALUES
            + "\n"
        )
        self.employee.write(
            {
                "l10n_pt_irs_jovem": True,
                "l10n_pt_irs_income_years": 4,  # 5th year -> 50%
            }
        )
        payslip = self._make_payslip()
        payslip.compute_sheet()
        lines = {line.code: line.total for line in payslip.line_ids}
        # exempt = min(1400 * 0.5, 55 * 522.50 / 14) = 700 -> base = 700
        # full-income retention 144.07 -> effective 10.291% on 700 -> 72
        self.assertAlmostEqual(lines["IRS"], -72.0)
        irs_line = payslip.line_ids.filtered(lambda line: line.code == "IRS")
        currency = self.env.company.currency_id
        self.assertIn(
            f"Taxable base: {format_amount(self.env, 700.0, currency)}",
            irs_line.name,
        )
        self.assertIn(
            f"income before relief: {format_amount(self.env, 1400.0, currency)}",
            irs_line.name,
        )
        self.assertIn("applied rate: 10.291%", irs_line.name)
        self.assertIn("monthly effective withholding rate: 5.14%", irs_line.name)
        # Above the annual cap the exemption stops at 55 x IAS / 14:
        # wage 5000 -> exempt 2052.68 -> base 2947.32, retention 596.
        self.employee.version_id.wage = 5000.0
        payslip = self._make_payslip()
        payslip.compute_sheet()
        self.employee.version_id.wage = 1400.0
        lines = {line.code: line.total for line in payslip.line_ids}
        self.assertAlmostEqual(lines["IRS"], -596.0)
        # First income year exempts the whole base: no rate is applied
        # and the name builder must not divide by the zero base.
        self.employee.l10n_pt_irs_income_years = 0
        payslip = self._make_payslip()
        payslip.compute_sheet()
        irs_line = payslip.line_ids.filtered(lambda line: line.code == "IRS")
        self.assertEqual(irs_line.total, 0.0)
        self.assertNotIn("applied rate", irs_line.name)
        self.assertIn("monthly effective withholding rate: 0.00%", irs_line.name)

    def test_irs_selects_table_at_period_end(self):
        """The payslip period end selects the withholding table: a
        period ending July 1 falls under the July table set."""
        # June work period paid July 1 uses the July table set.
        self.env["hr.pt.irs.withholding.import"]._import_csv_data(
            CSV_HEADER
            + "2026-07-01,2026-07-31,continente,I,0,,10,0,0,0,,"
            + SET_VALUES
            + "\n"
        )
        payslip = self._make_payslip()
        payslip.date_to = "2026-07-01"
        payslip.onchange_employee()
        payslip.compute_sheet()
        self.assertEqual(payslip.date_from.isoformat(), "2026-06-01")
        self.assertEqual(payslip.date_to.isoformat(), "2026-07-01")
        lines = {line.code: line.total for line in payslip.line_ids}
        # July overlay: single open bracket at 10% -> 1400 * 10% = 140
        self.assertAlmostEqual(lines["IRS"], -140.0)

    def test_irs_non_resident_single_payer_allowance(self):
        """CIRS art. 71: a non-resident with the written single-payer
        declaration on file may use part of the monthly minimum-wage
        allowance through the Other Inputs row; without the declaration
        the payslip refuses it."""
        self.employee.l10n_pt_irs_non_resident = True
        payslip = self._make_payslip()
        allowance = payslip.input_line_ids.filtered(
            lambda line: line.code == "PT_IRS_SINGLE_PAYER"
        )
        self.assertEqual(len(allowance), 1)
        allowance.amount = 920.0
        with self.assertRaises(UserError):
            payslip.compute_sheet()
        self.employee.l10n_pt_irs_single_payer_declared = True
        payslip.compute_sheet()
        lines = {line.code: line for line in payslip.line_ids}
        # base = 1400 - 920 = 480 -> 480 * 25% = 120
        self.assertAlmostEqual(lines["IRS"].total, -120.0)
        irs_line = lines["IRS"]
        currency = self.env.company.currency_id
        self.assertIn(
            f"Taxable base: {format_amount(self.env, 480.0, currency)}",
            irs_line.name,
        )
        self.assertIn(
            f"income before relief: {format_amount(self.env, 1400.0, currency)}",
            irs_line.name,
        )
        self.assertIn(
            f"single-payer allowance applied: "
            f"{format_amount(self.env, 920.0, currency)}",
            irs_line.name,
        )
        self.assertIn("monthly effective withholding rate: 8.57%", irs_line.name)
        # Hidden helper lines record this payment's taxable base and
        # the allowance actually applied.
        base_line = lines["PT_IRS_BASE"]
        self.assertAlmostEqual(base_line.total, 1400.0)
        self.assertAlmostEqual(base_line.amount, 1400.0)
        self.assertAlmostEqual(base_line.rate, 100.0)
        allowance_line = lines["PT_IRS_ALLOWANCE"]
        self.assertAlmostEqual(allowance_line.total, 920.0)
        for helper in (base_line, allowance_line):
            self.assertFalse(helper.appears_on_payslip)
        relieved = {code: lines[code].total for code in ("GROSS", "SS", "SS_COMP")}
        # A zero input leaves the full base taxable and does not touch
        # the earnings or Social Security lines.
        allowance.amount = 0.0
        payslip.compute_sheet()
        lines = {line.code: line for line in payslip.line_ids}
        self.assertAlmostEqual(lines["IRS"].total, -350.0)
        for code in ("GROSS", "SS", "SS_COMP"):
            self.assertAlmostEqual(lines[code].total, relieved[code])
        # Allowance larger than the payment leaves nothing to withhold.
        allowance.amount = 500.0
        self.employee.version_id.wage = 400.0
        payslip.compute_sheet()
        self.employee.version_id.wage = 1400.0
        lines = {line.code: line for line in payslip.line_ids}
        self.assertAlmostEqual(lines["IRS"].total, 0.0)
        irs_line = lines["IRS"]
        self.assertIn(
            f"single-payer allowance applied: "
            f"{format_amount(self.env, 400.0, currency)}",
            irs_line.name,
        )
        self.assertIn("monthly effective withholding rate: 0.00%", irs_line.name)
        # The recorded allowance is capped at the payment, not the
        # 500 entered on the input row.
        self.assertAlmostEqual(lines["PT_IRS_ALLOWANCE"].total, 400.0)

    def test_irs_single_payer_rejects_invalid_inputs(self):
        """The Other Inputs row rejects negative amounts and a second
        row for the same contract."""
        payslip = self._make_payslip()
        allowance = payslip.input_line_ids.filtered(
            lambda line: line.code == "PT_IRS_SINGLE_PAYER"
        )
        self.assertEqual(len(allowance), 1)
        allowance.amount = -50.0
        with self.assertRaisesRegex(UserError, "non-negative"):
            payslip.compute_sheet()
        allowance.amount = 0.0
        self.env["hr.payslip.input"].create(
            {
                "name": "Dedução de Entidade Única Neste Pagamento",
                "code": "PT_IRS_SINGLE_PAYER",
                "amount": 0.0,
                "contract_id": payslip.contract_id.id,
                "payslip_id": payslip.id,
            }
        )
        with self.assertRaisesRegex(UserError, "only one single-payer"):
            payslip.compute_sheet()

    def test_irs_accumulates_finalized_payments(self):
        """Done payslips accumulate the monthly withholding: each later
        slip books only the incremental amount on the month total."""
        self.employee.version_id.wage = 800.0
        first = self._make_payslip()
        first.payment_date = "2026-06-10"
        first.action_payslip_done()
        self.assertAlmostEqual(
            first.line_ids.filtered(lambda line: line.code == "IRS").total, -160.0
        )
        # The helper lines record each payment's own taxable base, not
        # the cumulative month total used inside the IRS calculation.
        first_lines = {line.code: line for line in first.line_ids}
        self.assertAlmostEqual(first_lines["PT_IRS_BASE"].amount, 800.0)
        self.assertAlmostEqual(first_lines["PT_IRS_ALLOWANCE"].total, 0.0)
        # The contract wage changed, but the finalized slip keeps its
        # recorded taxable base.
        self.employee.version_id.wage = 1400.0
        second = self._make_payslip()
        second.payment_date = "2026-06-20"
        second.compute_sheet()
        # Month total: (800 + 1400) taxable -> 2200 * 40% - 200 = 680,
        # less the 160 already withheld on the first slip.
        self.assertAlmostEqual(
            second.line_ids.filtered(lambda line: line.code == "IRS").total, -520.0
        )
        second_lines = {line.code: line for line in second.line_ids}
        self.assertAlmostEqual(second_lines["PT_IRS_BASE"].amount, 1400.0)
        self.assertEqual(second.state, "verify")
        third = self._make_payslip()
        third.payment_date = "2026-06-30"
        for _ in range(2):
            third.compute_sheet()
            self.assertAlmostEqual(
                third.line_ids.filtered(lambda line: line.code == "IRS").total, -520.0
            )
        third_irs = third.line_ids.filtered(lambda line: line.code == "IRS")
        currency = self.env.company.currency_id
        self.assertIn(
            f"month-to-date withholding: {format_amount(self.env, 680.0, currency)}",
            third_irs.name,
        )
        self.assertIn(
            f"previously withheld: {format_amount(self.env, 160.0, currency)}",
            third_irs.name,
        )
        # The finalized slip is untouched by later computations.
        self.assertAlmostEqual(
            first.line_ids.filtered(lambda line: line.code == "IRS").total, -160.0
        )

    def test_irs_accumulates_income_below_threshold(self):
        """Accumulation applies the monthly bracket to the combined
        income, so a second slip can cross into a taxed bracket."""
        self.env["hr.pt.irs.withholding.import"]._import_csv_data(
            CSV_HEADER
            + "2026-06-01,2026-06-30,continente,I,0,920,0,0,0,0,,"
            + SET_VALUES
            + "\n"
            + "2026-06-01,2026-06-30,continente,I,920,,24.1,193.33,0,0,,"
            + SET_VALUES
            + "\n"
        )
        self.employee.version_id.wage = 800.0
        first = self._make_payslip()
        first.payment_date = "2026-06-10"
        first.action_payslip_done()
        self.assertAlmostEqual(
            first.line_ids.filtered(lambda line: line.code == "IRS").total, 0.0
        )
        self.employee.version_id.wage = 700.0
        second = self._make_payslip()
        second.payment_date = "2026-06-20"
        second.compute_sheet()
        # Combined 1500 crosses the 920 threshold: 1500 * 24.1% - 193.33
        # = 168.17 -> 168 withheld now (nothing withheld before).
        self.assertAlmostEqual(
            second.line_ids.filtered(lambda line: line.code == "IRS").total, -168.0
        )
        # A payslip in another month does not see June's payments.
        july = self.env["hr.payslip"].create(
            {
                "employee_id": self.employee.id,
                "date_from": "2026-07-01",
                "date_to": "2026-07-31",
            }
        )
        july.onchange_employee()
        july.compute_sheet()
        july_irs = july.line_ids.filtered(lambda line: line.code == "IRS")
        self.assertAlmostEqual(july_irs.total, -140.0)
        self.assertNotIn("month-to-date", july_irs.name)
        self.employee.version_id.wage = 1400.0

    def test_irs_batch_confirmation_rejects_multiple_payments(self):
        """Confirming several payslips for the same employee and month
        in one batch is refused; each payment is confirmed on its own."""
        first = self._make_payslip()
        first.payment_date = "2026-06-20"
        second = self._make_payslip()
        second.payment_date = "2026-06-20"
        slips = self.env["hr.payslip"].browse([first.id, second.id])
        slips.compute_sheet()
        for slip in slips:
            self.assertAlmostEqual(
                slip.line_ids.filtered(lambda line: line.code == "IRS").total, -280.0
            )
        # A batch state write covering both slips is refused by the
        # payment-order constraint (action_payslip_done is singleton).
        with self.assertRaisesRegex(ValidationError, "separately"), self.cr.savepoint():
            slips.write({"state": "done"})
        self.assertNotEqual(first.state, "done")
        self.assertNotEqual(second.state, "done")
        # Confirmed one at a time, the same payments accumulate normally.
        first.action_payslip_done()
        second.action_payslip_done()
        self.assertEqual(first.state, "done")
        self.assertEqual(second.state, "done")
        # Second slip: combined taxable 2800 -> 2800 * 40% - 200 = 920,
        # less the 280 withheld on the first.
        self.assertAlmostEqual(
            first.line_ids.filtered(lambda line: line.code == "IRS").total, -280.0
        )
        self.assertAlmostEqual(
            second.line_ids.filtered(lambda line: line.code == "IRS").total, -640.0
        )

    def test_irs_blocks_backdated_payslip(self):
        """A payslip ending before an already-finalized slip in the same
        month is refused for manual review."""
        first = self._make_payslip()
        first.payment_date = "2026-06-30"
        first.action_payslip_done()
        earlier = self.env["hr.payslip"].create(
            {
                "employee_id": self.employee.id,
                "date_from": "2026-06-01",
                "date_to": "2026-06-15",
            }
        )
        earlier.payment_date = "2026-06-15"
        earlier.onchange_employee()
        with self.assertRaisesRegex(UserError, "backdated"):
            earlier.compute_sheet()

    def test_irs_uses_explicit_payment_month(self):
        """Payment Date selects the withholding month: a June payslip
        paid in July accumulates with other July payments."""
        self.env["hr.pt.irs.withholding.import"]._import_csv_data(
            CSV_HEADER
            + "2026-07-01,2026-07-31,continente,I,0,,10,0,0,0,,"
            + SET_VALUES
            + "\n"
        )
        first = self._make_payslip()
        first.payment_date = "2026-07-01"
        first.action_payslip_done()
        self.assertAlmostEqual(
            first.line_ids.filtered(lambda line: line.code == "IRS").total, -140.0
        )
        second = self._make_payslip()
        second.payment_date = "2026-07-02"
        second.compute_sheet()
        # July month total (1400 + 1400) * 10% = 280, less 140 withheld.
        second_irs = second.line_ids.filtered(lambda line: line.code == "IRS")
        self.assertAlmostEqual(second_irs.total, -140.0)
        currency = self.env.company.currency_id
        self.assertIn(
            f"month-to-date withholding: {format_amount(self.env, 280.0, currency)}",
            second_irs.name,
        )
        self.assertIn(
            f"previously withheld: {format_amount(self.env, 140.0, currency)}",
            second_irs.name,
        )
        # Blank Payment Date falls back to the period end (June), which
        # sees none of the July payments.
        third = self._make_payslip()
        third.compute_sheet()
        third_irs = third.line_ids.filtered(lambda line: line.code == "IRS")
        self.assertAlmostEqual(third_irs.total, -280.0)
        self.assertNotIn("month-to-date", third_irs.name)

    def test_irs_blocks_payment_date_before_confirmed_payment(self):
        """A slip paid before an already-confirmed payment for the same
        employee, company, and month is refused for manual review."""
        late = self._make_payslip()
        late.payment_date = "2026-07-15"
        late.action_payslip_done()
        early = self._make_payslip()
        early.payment_date = "2026-07-01"
        with self.assertRaisesRegex(UserError, "backdated"):
            with self.cr.savepoint():
                early.action_payslip_done()
        self.assertNotEqual(early.state, "done")

    def test_irs_requires_explicit_dates_for_multiple_payments(self):
        """A lone monthly payslip may leave Payment Date blank; once a
        month has several payments every payslip needs an explicit
        date, whichever side is confirmed first."""
        lone = self._make_payslip()
        lone.action_payslip_done()
        self.assertEqual(lone.state, "done")
        self.assertAlmostEqual(
            lone.line_ids.filtered(lambda line: line.code == "IRS").total, -280.0
        )
        # A second June payment conflicts because the confirmed slip
        # has no Payment Date — even an explicitly dated slip is refused.
        june = self._make_payslip()
        june.payment_date = "2026-06-15"
        with self.assertRaisesRegex(UserError, "Set a payment date"):
            with self.cr.savepoint():
                june.action_payslip_done()
        self.assertNotEqual(june.state, "done")
        # Reverse order in July: the explicit payment confirms first,
        # then the blank payslip for the same month is refused.
        july_paid = self.env["hr.payslip"].create(
            {
                "employee_id": self.employee.id,
                "date_from": "2026-07-01",
                "date_to": "2026-07-31",
            }
        )
        july_paid.payment_date = "2026-07-10"
        july_paid.onchange_employee()
        july_paid.action_payslip_done()
        self.assertEqual(july_paid.state, "done")
        july_blank = self.env["hr.payslip"].create(
            {
                "employee_id": self.employee.id,
                "date_from": "2026-07-01",
                "date_to": "2026-07-31",
            }
        )
        july_blank.onchange_employee()
        with self.assertRaisesRegex(UserError, "Set a payment date"):
            with self.cr.savepoint():
                july_blank.action_payslip_done()
        self.assertNotEqual(july_blank.state, "done")

    def test_irs_retention_no_table(self):
        """A payslip outside every imported table period fails loudly
        instead of silently using a stale rate."""
        payslip = self.env["hr.payslip"].create(
            {
                "employee_id": self.employee.id,
                "date_from": "2027-06-01",
                "date_to": "2027-06-30",
            }
        )
        payslip.onchange_employee()
        with self.assertRaises(UserError):
            payslip.compute_sheet()

    def test_payment_method_is_versioned_and_inert(self):
        """The salary payment method lives on the contract version: it
        starts unset, an employee write updates the current version, a
        dated version change preserves earlier values, and none of it
        touches bank accounts."""
        employee = self.env["hr.employee"].create(
            {
                "name": "Payment Method Employee",
                "company_id": self.env.company.id,
                "date_version": "2026-01-01",
            }
        )
        version = employee.version_id
        self.assertFalse(version.l10n_pt_payment_method)
        self.assertFalse(employee.bank_account_ids)

        employee.write({"l10n_pt_payment_method": "cheque"})
        self.assertEqual(version.l10n_pt_payment_method, "cheque")
        self.assertFalse(employee.bank_account_ids)

        new_version = employee.create_version(
            {
                "date_version": "2026-11-01",
                "l10n_pt_payment_method": "cash",
            }
        )
        self.assertNotEqual(new_version, version)
        self.assertEqual(new_version.l10n_pt_payment_method, "cash")
        self.assertEqual(
            employee._get_version("2026-10-31").l10n_pt_payment_method,
            "cheque",
        )
        self.assertFalse(employee.bank_account_ids)

    def test_payment_method_id_is_versioned_and_validated(self):
        """The Accounting payment method lives on the contract version
        next to the legacy selection: it starts unset, an employee
        write updates the current version, a dated version change can
        clear it while earlier dates keep theirs, and only outbound
        methods are accepted."""
        employee = self.env["hr.employee"].create(
            {
                "name": "Payment Method Id Employee",
                "company_id": self.env.company.id,
                "date_version": "2026-01-01",
                "l10n_pt_payment_method": "cheque",
            }
        )
        manual_out = self.env.ref("account.account_payment_method_manual_out")
        manual_in = self.env.ref("account.account_payment_method_manual_in")
        version = employee.version_id
        self.assertFalse(employee.payment_method_id)
        self.assertEqual(employee.l10n_pt_payment_method, "cheque")
        self.assertFalse(employee.bank_account_ids)

        employee.write({"payment_method_id": manual_out.id})
        self.assertEqual(version.payment_method_id, manual_out)
        self.assertEqual(employee.l10n_pt_payment_method, "cheque")
        self.assertFalse(employee.bank_account_ids)

        new_version = employee.create_version(
            {
                "date_version": "2026-11-01",
                "payment_method_id": False,
            }
        )
        self.assertNotEqual(new_version, version)
        self.assertFalse(new_version.payment_method_id)
        self.assertEqual(
            employee._get_version("2026-10-31").payment_method_id,
            manual_out,
        )
        self.assertFalse(employee.bank_account_ids)

        with self.assertRaises(ValidationError):
            employee.version_id.write({"payment_method_id": manual_in.id})


class TestL10nPtSalaryBankAccount(TransactionCase):
    """The salary bank account lives on the contract version: an HR
    manager picks one of the employee's standard bank accounts, invalid
    or foreign accounts are rejected, history is preserved, and the Add
    Bank Account wizard creates, links, and selects a new account on the
    employee's work contact."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.employee = cls.env["hr.employee"].create(
            {
                "name": "Salary Bank Employee",
                "company_id": cls.env.company.id,
                "date_version": "2026-01-01",
            }
        )
        contact = cls.employee.work_contact_id
        cls.account_primary = cls.env["res.partner.bank"].create(
            {
                "partner_id": contact.id,
                "acc_number": "PT50000000000000000000001",
            }
        )
        cls.account_secondary = cls.env["res.partner.bank"].create(
            {
                "partner_id": contact.id,
                "acc_number": "PT50000000000000000000002",
            }
        )
        cls.account_unlinked = cls.env["res.partner.bank"].create(
            {
                "partner_id": contact.id,
                "acc_number": "PT50000000000000000000003",
            }
        )
        other_partner = cls.env["res.partner"].create({"name": "Other Owner"})
        cls.account_foreign = cls.env["res.partner.bank"].create(
            {
                "partner_id": other_partner.id,
                "acc_number": "PT50000000000000000000004",
            }
        )
        cls.employee.write(
            {
                "bank_account_ids": [
                    Command.link(cls.account_primary.id),
                    Command.link(cls.account_secondary.id),
                ]
            }
        )

    def _hr_manager(self, with_partner_manager=True):
        groups = [
            self.env.ref("base.group_user"),
            self.env.ref("hr.group_hr_manager"),
        ]
        if with_partner_manager:
            groups.append(self.env.ref("base.group_partner_manager"))
        return self.env["res.users"].create(
            {
                "name": "Salary Bank HR Manager",
                "login": f"salary_bank_hr_manager_{with_partner_manager}",
                "company_id": self.env.company.id,
                "company_ids": [Command.set([self.env.company.id])],
                "group_ids": [Command.set(group.id for group in groups)],
            }
        )

    def test_payment_bank_account_is_versioned_and_inert(self):
        """An employee write selects the account on the current version,
        a dated version change keeps earlier selections, and archiving
        or unlinking an account never clears stored history."""
        employee = self.employee
        version = employee.version_id
        self.assertFalse(employee.payment_bank_account_id)

        employee.write({"payment_bank_account_id": self.account_primary.id})
        self.assertEqual(version.payment_bank_account_id, self.account_primary)
        self.assertEqual(employee.payment_bank_account_id, self.account_primary)

        new_version = employee.create_version(
            {
                "date_version": "2026-11-01",
                "payment_bank_account_id": self.account_secondary.id,
            }
        )
        self.assertNotEqual(new_version, version)
        self.assertEqual(new_version.payment_bank_account_id, self.account_secondary)
        self.assertEqual(
            employee._get_version("2026-10-31").payment_bank_account_id,
            self.account_primary,
        )

        self.account_primary.write({"active": False})
        self.assertFalse(self.account_primary.active)
        self.assertEqual(
            employee._get_version("2026-10-31").payment_bank_account_id,
            self.account_primary,
        )

        employee.write({"bank_account_ids": [Command.unlink(self.account_primary.id)]})
        self.assertEqual(
            employee._get_version("2026-10-31").payment_bank_account_id,
            self.account_primary,
        )

    def test_payment_bank_account_rejects_invalid_accounts(self):
        """ORM writes reject a same-contact account missing from the
        employee's Bank Accounts and an account owned by another
        partner, bypassing the view domain."""
        with self.assertRaises(ValidationError):
            self.employee.write({"payment_bank_account_id": self.account_unlinked.id})
        with self.assertRaises(ValidationError):
            self.employee.write({"payment_bank_account_id": self.account_foreign.id})
        self.assertFalse(self.employee.payment_bank_account_id)

    def test_add_bank_account_wizard(self):
        """The Add Bank Account wizard, bound to a version, creates a
        standard account on the work contact, links it to the employee,
        selects it on that version, and keeps the existing bank
        accounts untouched."""
        hr_manager = self._hr_manager()
        employee = self.employee
        version = employee.version_id
        previous_accounts = employee.bank_account_ids

        blank_wizard = (
            self.env["hr.payroll.bank.account.add"]
            .with_user(hr_manager)
            .with_context(default_version_id=version.id)
            .create({"acc_number": "   "})
        )
        with self.assertRaises(UserError):
            blank_wizard.action_add_bank_account()

        wizard = (
            self.env["hr.payroll.bank.account.add"]
            .with_user(hr_manager)
            .with_context(default_version_id=version.id)
            .create({"acc_number": "PT50000000000000000000009"})
        )
        result = wizard.with_context(
            default_allow_out_payment=True
        ).action_add_bank_account()
        self.assertEqual(result["tag"], "soft_reload")

        self.env.invalidate_all()
        employee = self.env["hr.employee"].browse(employee.id)
        version = employee.version_id
        account = employee.work_contact_id.bank_ids.filtered(
            lambda bank: bank.acc_number == "PT50000000000000000000009"
        )
        self.assertEqual(len(account), 1)
        self.assertFalse(account.allow_out_payment)
        self.assertIn(account, employee.bank_account_ids)
        self.assertTrue(previous_accounts <= employee.bank_account_ids)
        self.assertEqual(version.payment_bank_account_id, account)
        self.assertEqual(employee.payment_bank_account_id, account)

    def test_add_bank_account_wizard_requires_rights(self):
        """Only HR managers can use the employee action, and creating
        the bank account still needs native Partner Creation rights."""
        plain_user = self.env["res.users"].create(
            {
                "name": "Plain Internal User",
                "login": "salary_bank_plain_user",
                "company_id": self.env.company.id,
                "company_ids": [Command.set([self.env.company.id])],
                "group_ids": [Command.set([self.env.ref("base.group_user").id])],
            }
        )
        with self.assertRaises(AccessError):
            self.employee.with_user(plain_user).action_add_payment_bank_account()

        hr_only = self._hr_manager(with_partner_manager=False)
        wizard = (
            self.env["hr.payroll.bank.account.add"]
            .with_user(hr_only)
            .with_context(default_version_id=self.employee.version_id.id)
            .create({"acc_number": "PT50000000000000000000010"})
        )
        with self.assertRaises(AccessError):
            wizard.action_add_bank_account()

    def test_add_bank_account_button_native_form(self):
        """The Add Bank Account shortcut opens the same dedicated bank
        form as the selector and, through its context, selects the new
        account on the version immediately."""
        hr_manager = self._hr_manager()
        employee = self.employee
        version = employee.version_id
        contact = employee.work_contact_id
        bank = self.env["res.bank"].search([], limit=1) or self.env["res.bank"].create(
            {"name": "Selector Test Bank"}
        )

        action = employee.with_user(hr_manager).action_add_payment_bank_account()
        bank_view = self.env.ref("l10n_pt_payroll.view_payroll_partner_bank_form")
        self.assertEqual(action["res_model"], "res.partner.bank")
        self.assertEqual(action["views"], [(bank_view.id, "form")])
        self.assertEqual(action["target"], "new")
        self.assertEqual(action["context"]["default_partner_id"], contact.id)
        self.assertEqual(
            action["context"]["payroll_bank_account_version_id"], version.id
        )
        self.assertTrue(action["context"]["payroll_select_bank_account"])

        account = (
            self.env["res.partner.bank"]
            .with_user(hr_manager)
            .with_context(**action["context"])
            .create(
                {
                    "partner_id": contact.id,
                    "acc_number": "PT50000000000000000000015",
                    "bank_id": bank.id,
                }
            )
        )
        self.env.invalidate_all()
        employee = self.env["hr.employee"].browse(employee.id)
        version = employee.version_id
        self.assertEqual(account.partner_id, contact)
        self.assertEqual(account.bank_id, bank)
        self.assertIn(account.id, employee.bank_account_ids.ids)
        self.assertEqual(version.payment_bank_account_id, account)
        self.assertEqual(employee.payment_bank_account_id, account)

    def test_bank_account_selector_native_create(self):
        """The Salary Bank Account selector's Create dialog goes through
        res.partner.bank.create with the payroll context: the account is
        pinned to the employee's work contact, never trusted, joins the
        contact and employee bank lists, and the stored version
        selection only changes when the employee form is saved."""
        hr_manager = self._hr_manager()
        employee = self.employee
        version = employee.version_id
        contact = employee.work_contact_id
        employee.write({"payment_bank_account_id": self.account_primary.id})

        account = (
            self.env["res.partner.bank"]
            .with_user(hr_manager)
            .with_context(
                default_partner_id=contact.id,
                payroll_bank_account_version_id=version.id,
            )
            .create(
                {
                    "partner_id": self.account_foreign.partner_id.id,
                    "acc_number": "PT50000000000000000000011",
                    "allow_out_payment": True,
                }
            )
        )
        self.env.invalidate_all()
        employee = self.env["hr.employee"].browse(employee.id)
        version = employee.version_id
        contact = employee.work_contact_id
        self.assertEqual(account.partner_id, contact)
        self.assertTrue(account.active)
        self.assertFalse(account.allow_out_payment)
        self.assertIn(account.id, contact.bank_ids.ids)
        self.assertIn(account.id, employee.bank_account_ids.ids)
        # Creating from the dialog does not select the account by itself.
        self.assertEqual(version.payment_bank_account_id, self.account_primary)

        employee.write({"payment_bank_account_id": account.id})
        self.assertEqual(version.payment_bank_account_id, account)

        # Without the payroll context, creation stays a plain bank account.
        plain_account = self.env["res.partner.bank"].create(
            {"partner_id": contact.id, "acc_number": "PT50000000000000000000012"}
        )
        self.assertNotIn(plain_account.id, employee.bank_account_ids.ids)

        # Users outside the payroll flow cannot use the context flag.
        plain_user = self.env["res.users"].create(
            {
                "name": "Plain Internal User Native",
                "login": "salary_bank_plain_user_native",
                "company_id": self.env.company.id,
                "company_ids": [Command.set([self.env.company.id])],
                "group_ids": [Command.set([self.env.ref("base.group_user").id])],
            }
        )
        with self.assertRaises(AccessError):
            (
                self.env["res.partner.bank"]
                .with_user(plain_user)
                .with_context(payroll_bank_account_version_id=version.id)
                .create(
                    {
                        "partner_id": contact.id,
                        "acc_number": "PT50000000000000000000013",
                    }
                )
            )
        hr_only = self._hr_manager(with_partner_manager=False)
        with self.assertRaises(AccessError):
            (
                self.env["res.partner.bank"]
                .with_user(hr_only)
                .with_context(payroll_bank_account_version_id=version.id)
                .create(
                    {
                        "partner_id": contact.id,
                        "acc_number": "PT50000000000000000000014",
                    }
                )
            )

    def test_salary_bank_account_selector_view(self):
        """The Salary Bank Account field opens the dedicated bank form
        in create/edit dialogs and stays read-only until the employee
        has a saved record with a work contact."""
        arch = self.env.ref(
            "l10n_pt_payroll.view_employee_form_l10n_pt_payroll"
        ).get_combined_arch()
        root = ET.fromstring(arch)
        node = root.find(".//field[@name='payment_bank_account_id']")
        self.assertIsNotNone(node)
        self.assertEqual(node.get("readonly"), "not id or not work_contact_id")
        context = node.get("context") or ""
        self.assertIn("payroll_bank_account_version_id", context)
        self.assertIn("form_view_ref", context)
        self.assertIn("l10n_pt_payroll.view_payroll_partner_bank_form", context)
        options = node.get("options") or ""
        self.assertIn("no_quick_create", options)
        self.assertIn("no_open", options)
        self.assertNotIn("'no_create'", options)

        bank_view = self.env.ref("l10n_pt_payroll.view_payroll_partner_bank_form")
        self.assertEqual(bank_view.model, "res.partner.bank")
        self.assertEqual(bank_view.priority, 100)
        default_view = self.env["ir.ui.view"].search(
            [("model", "=", "res.partner.bank"), ("type", "=", "form")],
            order="priority, id",
            limit=1,
        )
        self.assertNotEqual(default_view, bank_view)


class TestL10nPtPayrollIrs(TransactionCase):
    def test_irs_table_derivation(self):
        """The IRS withholding table variant is derived from the declared
        determinants: family situation, IRS dependents and disability."""
        employee = self.env["hr.employee"].create(
            {
                "name": "IRS Employee 1",
                "l10n_pt_irs_family_status": "married_single_income",
            }
        )
        self.assertEqual(
            employee.l10n_pt_irs_table_id,
            self.env.ref("l10n_pt_hr.l10n_pt_irs_table_3"),
        )
        # Disability >= 60% switches to the disabled table variant
        employee.l10n_pt_disability_degree = 65
        self.assertEqual(
            employee.l10n_pt_irs_table_id,
            self.env.ref("l10n_pt_hr.l10n_pt_irs_table_7"),
        )
        # A single worker with dependents uses table II
        employee.write(
            {
                "l10n_pt_irs_family_status": "single",
                "l10n_pt_disability_degree": 0,
                "l10n_pt_irs_dependents": 2,
            }
        )
        self.assertEqual(
            employee.l10n_pt_irs_table_id,
            self.env.ref("l10n_pt_hr.l10n_pt_irs_table_2"),
        )
        # Without a declared family situation no table applies
        employee.l10n_pt_irs_family_status = False
        self.assertFalse(employee.l10n_pt_irs_table_id)

    def test_irs_table_derivation_pensioner(self):
        """A pensioner resolves the pension table family (VIII-XI),
        which only distinguishes family situation and disability — not
        dependents."""
        employee = self.env["hr.employee"].create(
            {
                "name": "Pensioner Employee",
                "l10n_pt_irs_pensioner": True,
                "l10n_pt_irs_family_status": "single",
                "l10n_pt_irs_dependents": 3,
            }
        )
        self.assertEqual(
            employee.l10n_pt_irs_table_id,
            self.env.ref("l10n_pt_hr.l10n_pt_irs_table_8"),
        )
        # Disabled pensioner with a single-income spouse uses table XI
        employee.write(
            {
                "l10n_pt_irs_family_status": "married_single_income",
                "l10n_pt_disability_degree": 70,
            }
        )
        self.assertEqual(
            employee.l10n_pt_irs_table_id,
            self.env.ref("l10n_pt_hr.l10n_pt_irs_table_11"),
        )
        # Non-pensioner with the same determinants uses the work tables
        employee.l10n_pt_irs_pensioner = False
        self.assertEqual(
            employee.l10n_pt_irs_table_id,
            self.env.ref("l10n_pt_hr.l10n_pt_irs_table_7"),
        )

    def test_irs_region_from_address(self):
        """The IRS region defaults from the employee's address: PT state
        districts first, postal code prefix as fallback, and a manual
        choice is kept."""
        employee = self.env["hr.employee"].create(
            {
                "name": "IRS Employee 2",
                "private_state_id": self.env.ref("base.state_pt_pt-20").id,
            }
        )
        self.assertEqual(employee.l10n_pt_irs_region, "acores")
        employee.private_state_id = self.env.ref("base.state_pt_pt-30")
        # region was auto-filled once; a manual value wins over recompute
        self.assertEqual(employee.l10n_pt_irs_region, "acores")
        employee.l10n_pt_irs_region = "madeira"
        employee.private_zip = "4000-001"
        self.assertEqual(employee.l10n_pt_irs_region, "madeira")


class TestL10nPtPayrollIrsImport(TransactionCase):
    def test_import_csv_creates_table_sets(self):
        """A valid CSV creates one withholding table set per
        (period, region, variant) block with its brackets."""
        # 2030 keeps the fixture clear of the bundled 2026 tables.
        csv_text = CSV_HEADER + (
            "2030-01-01,2030-12-31,acores,I,0,1500,18,0,30,50,,522.50\n"
            "2030-01-01,2030-12-31,acores,I,1500,,40,200,30,50,,522.50\n"
            "2030-01-01,2030-12-31,madeira,II,0,,25,100,30,50,,522.50\n"
        )
        count = self.env["hr.pt.irs.withholding.import"]._import_csv_data(csv_text)
        self.assertEqual(count, 2)
        tables = self.env["hr.pt.irs.withholding.table"].search(
            [
                ("region", "in", ["acores", "madeira"]),
                ("date_start", "=", "2030-01-01"),
            ]
        )
        self.assertEqual(len(tables), 2)
        acores = tables.filtered(lambda t: t.region == "acores")
        self.assertEqual(len(acores.line_ids), 2)
        self.assertAlmostEqual(acores.ias, 522.50)

    def test_import_csv_replaces_existing_lines(self):
        """Re-importing the same block replaces its brackets instead of
        duplicating them."""
        # 2030 keeps the fixture clear of the bundled 2026 tables.
        csv_text = CSV_HEADER + (
            "2030-01-01,2030-12-31,continente,I,0,,20,0,0,0,,522.50\n"
        )
        model = self.env["hr.pt.irs.withholding.import"]
        model._import_csv_data(csv_text)
        corrected = csv_text.replace(",20,0,0,0,", ",22.5,0,0,0,")
        model._import_csv_data(corrected)
        table = self.env["hr.pt.irs.withholding.table"].search(
            [
                ("region", "=", "continente"),
                ("table_id", "=", self.env.ref("l10n_pt_hr.l10n_pt_irs_table_1").id),
            ],
            limit=1,
        )
        self.assertEqual(len(table.line_ids), 1)
        self.assertAlmostEqual(table.line_ids.marginal_rate, 22.5)

    def test_import_csv_validation_errors(self):
        """Invalid files are rejected: gaps in the brackets, unknown
        regions or table codes, out-of-range rates."""
        model = self.env["hr.pt.irs.withholding.import"]
        with self.assertRaises(UserError):
            # Gap between brackets
            model._import_csv_data(
                CSV_HEADER
                + "2026-01-01,2026-12-31,continente,I,0,1000,20,0,0,0,,1\n"
                + "2026-01-01,2026-12-31,continente,I,2000,,40,0,0,0,,1\n"
            )
        with self.assertRaises(UserError):
            # Unknown region
            model._import_csv_data(
                CSV_HEADER + "2026-01-01,2026-12-31,atlantico,I,0,,20,0,0,0,,1\n"
            )
        with self.assertRaises(UserError):
            # Rate above 100
            model._import_csv_data(
                CSV_HEADER + "2026-01-01,2026-12-31,continente,I,0,,150,0,0,0,,1\n"
            )
        with self.assertRaises(UserError):
            # Missing required column
            model._import_csv_data(
                "date_start,region,table_code\n2026-01-01,continente,I\n"
            )
