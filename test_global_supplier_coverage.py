"""Evidence coverage and worldwide trade visibility; no external requests."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import countries as CO
import country_rank as CR
import supplier_catalogue as S
import supplier_refresh as R
import procurement_workflow as W


class CoverageTests(unittest.TestCase):
    def test_regional_records_use_known_countries_materials_and_evidence(self):
        data=S.load()
        ids=[s["id"] for s in data["suppliers"]]
        self.assertEqual(len(ids),len(set(ids)))
        for s in data["suppliers"]:
            self.assertIsNotNone(CO.get(s["country"]),s["id"])
            self.assertIn(s["material"],("ZINC_SHG","STEEL_HRC"),s["id"])
            self.assertTrue(s["evidence"])
            if s.get("contact_url"):
                self.assertEqual(urlsplit(s["contact_url"]).scheme,"https")
                self.assertTrue(s["contact_evidence"])
                self.assertEqual(s["order_status"],"rfq_channel_published")
                self.assertEqual(s["origin_status"],"unconfirmed")

    def test_singapore_finland_visible_without_being_shortlisted(self):
        result=W.build(W.validate({"material":"STEEL_HRC"}),None,[])
        groups={g["country"]:g for g in result["supplier_catalogue"]}
        self.assertEqual(len(groups),len(CO._C))
        self.assertGreaterEqual(len(groups["SGP"]["suppliers"]),4)
        self.assertGreaterEqual(len(groups["FIN"]["suppliers"]),3)
        for c in groups.values():
            for supplier in c["suppliers"]:
                self.assertIsNone(supplier["quote_per_t"])
                self.assertIsNone(supplier["delivery_days"])
                self.assertIsNone(supplier["quantity_discount"])

    def test_no_padding_of_finnish_or_singapore_zinc(self):
        result=W.build(W.validate({}),None,[])
        groups={g["country"]:g for g in result["supplier_catalogue"]}
        self.assertEqual(len(groups["FIN"]["suppliers"]),1)
        self.assertEqual(groups["FIN"]["supplier_gap"],2)
        self.assertEqual(len(groups["SGP"]["suppliers"]),2)
        self.assertEqual(groups["SGP"]["supplier_gap"],1)

    def test_daily_monitor_includes_new_products_and_contacts(self):
        targets=set(R.targets())
        for supplier in S.load()["suppliers"]:
            self.assertIn(supplier["url"],targets)
            if supplier.get("contact_url"):self.assertIn(supplier["contact_url"],targets)

    def test_worldwide_view_preserves_small_unmapped_partner_rates(self):
        flow=dict(partner_code=32,partner="Argentina",partner_iso="ARG",value_usd=60000,
                  net_weight_kg=20000,unit_value_usd_per_t=3000)
        with patch.object(CR,"imports_by_partner_any_year",return_value=([flow],2025)):
            ranked=CR.rank_source_countries("IND","790111",year=2026)
        self.assertEqual(list(ranked),[])  # Outside mapped tariff registry; not a fake landed-price rank.
        self.assertEqual(ranked.worldwide_rates[0]["name"],"Argentina")
        self.assertEqual(ranked.worldwide_rates[0]["unit_value_usd_per_t"],3000)
        self.assertFalse(ranked.worldwide_rates[0]["tariff_registry_supported"])
        self.assertEqual(ranked.worldwide_rates[0]["year"],2025)

    def test_supplementary_pieces_are_not_mislabelled_as_kilograms(self):
        rows=[dict(period=2026,partnerCode=32,primaryValue=10000,netWgt=0,qty=20,qtyUnitCode=5)]
        response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{"data":rows})
        with patch.object(CR.requests,"get",return_value=response):
            flows=CR._imports_by_partner(699,"7901",2026)
        self.assertIsNone(flows[0]["unit_value_usd_per_t"])
        self.assertEqual(flows[0]["net_weight_kg"],0)
