import json
import re
import unittest
from html.parser import HTMLParser


class StandardHTMLMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_json_ld = False
        self.json_ld_texts = []
        self.meta_tags = []

    def handle_starttag(self, tag, attrs):
        attr_dict = {k.lower(): v for k, v in attrs if k}
        if tag == "script" and attr_dict.get("type") == "application/ld+json":
            self.in_json_ld = True
        elif tag == "meta":
            prop = attr_dict.get("property") or attr_dict.get("name")
            content = attr_dict.get("content")
            if prop and content:
                self.meta_tags.append({"property": prop.lower(), "content": content})

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_json_ld = False

    def handle_data(self, data):
        if self.in_json_ld and data.strip():
            self.json_ld_texts.append(data.strip())


def extract_json_ld_from_html(html_text: str) -> list[dict]:
    parser = StandardHTMLMetadataParser()
    parser.feed(html_text)
    results = []

    for text in parser.json_ld_texts:
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else (data.get("@graph", [data]) if isinstance(data, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                type_str = str(it.get("@type", ""))
                if any(k.lower() in type_str.lower() for k in ("product", "offer", "service")):
                    name = it.get("name") or "Gói dịch vụ"
                    offers = it.get("offers")
                    price = None
                    currency = "VND"
                    if isinstance(offers, dict):
                        price = offers.get("price") or offers.get("lowPrice")
                        currency = offers.get("priceCurrency", currency)
                    elif isinstance(offers, list) and offers:
                        price = offers[0].get("price") or offers[0].get("lowPrice")
                        currency = offers[0].get("priceCurrency", currency)
                    elif it.get("price"):
                        price = it.get("price")
                        currency = it.get("priceCurrency", currency)

                    if price is not None:
                        results.append({
                            "name": name,
                            "price_raw": f"{price} {currency}",
                            "description": it.get("description", ""),
                            "source": "json-ld"
                        })
        except Exception:
            continue

    return results


class TestJsonLdExtraction(unittest.TestCase):
    def test_extracts_product_with_offer(self):
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Competitor Cloud Hosting</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Cloud Hosting Enterprise",
                "description": "High performance 4GB RAM 50GB SSD",
                "offers": {
                    "@type": "Offer",
                    "price": "350000",
                    "priceCurrency": "VND",
                    "availability": "https://schema.org/InStock"
                }
            }
            </script>
        </head>
        <body>
            <h1>Hosting Enterprise</h1>
        </body>
        </html>
        """
        extracted = extract_json_ld_from_html(sample_html)
        self.assertGreaterEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["name"], "Cloud Hosting Enterprise")
        self.assertIn("350000", extracted[0]["price_raw"])


if __name__ == "__main__":
    unittest.main()
