import tempfile
import unittest
from pathlib import Path

from app import (
    LOGIN_PASSWORD,
    LOGIN_USERNAME,
    all_interface_keys,
    create_item,
    create_user,
    create_sale,
    customer_sales,
    db_session,
    delete_item,
    default_path_for_user,
    get_customer,
    get_item,
    init_db,
    list_customers,
    list_items,
    search_sales,
    secure_text_equal,
    update_item,
    user_can_access,
    user_permission_keys,
)


class CashierDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_item_crud(self):
        with db_session(self.db_path) as db:
            item_id = create_item(db, "شاي", "T-1", "مشروبات", 1000, 12)
            item = get_item(db, item_id)
            self.assertEqual(item["name"], "شاي")
            self.assertEqual(item["stock"], 12)

            update_item(db, item_id, "شاي عراقي", "T-1", "مشروبات", 1250, 9)
            updated = get_item(db, item_id)
            self.assertEqual(updated["name"], "شاي عراقي")
            self.assertEqual(updated["price"], 1250)

            delete_item(db, item_id)
            self.assertEqual(list_items(db), [])

    def test_create_sale_reduces_stock_and_records_total(self):
        with db_session(self.db_path) as db:
            tea_id = create_item(db, "شاي", "T-1", "مشروبات", 1000, 12)
            cake_id = create_item(db, "كيك", "C-1", "حلويات", 2500, 4)

            sale_id, total, change = create_sale(db, {tea_id: 2, cake_id: 1}, paid=5000)

            self.assertEqual(sale_id, 1)
            self.assertEqual(total, 4500)
            self.assertEqual(change, 500)
            self.assertEqual(get_item(db, tea_id)["stock"], 10)
            self.assertEqual(get_item(db, cake_id)["stock"], 3)

            lines = db.execute("SELECT COUNT(*) FROM sale_items WHERE sale_id = ?", (sale_id,)).fetchone()[0]
            self.assertEqual(lines, 2)

    def test_sale_rejects_insufficient_payment_and_stock(self):
        with db_session(self.db_path) as db:
            item_id = create_item(db, "ماء", "W-1", "مشروبات", 500, 1)

            with self.assertRaises(ValueError):
                create_sale(db, {item_id: 1}, paid=250)

            with self.assertRaises(ValueError):
                create_sale(db, {item_id: 2}, paid=1000)

    def test_create_debt_sale_records_customer_and_debt(self):
        with db_session(self.db_path) as db:
            item_id = create_item(db, "رز", "R-1", "مواد غذائية", 3000, 5)

            sale_id, total, change = create_sale(
                db,
                {item_id: 2},
                paid=1000,
                payment_type="debt",
                customer_name="أحمد",
            )

            self.assertEqual(total, 6000)
            self.assertEqual(change, 0)
            self.assertEqual(get_item(db, item_id)["stock"], 3)

            sale = db.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
            self.assertEqual(sale["payment_type"], "debt")
            self.assertEqual(sale["customer_name"], "أحمد")
            self.assertIsNotNone(sale["customer_id"])
            self.assertEqual(sale["debt_amount"], 5000)
            self.assertEqual(get_customer(db, sale["customer_id"])["name"], "أحمد")

    def test_debt_sale_requires_customer_name(self):
        with db_session(self.db_path) as db:
            item_id = create_item(db, "رز", "R-1", "مواد غذائية", 3000, 5)

            with self.assertRaises(ValueError):
                create_sale(db, {item_id: 1}, paid=0, payment_type="debt")

            with self.assertRaises(ValueError):
                create_sale(db, {item_id: 1}, paid=3000, payment_type="debt", customer_name="أحمد")

    def test_cash_sale_with_customer_appears_in_customer_statement(self):
        with db_session(self.db_path) as db:
            item_id = create_item(db, "شاي", "T-1", "مشروبات", 1000, 4)

            sale_id, total, change = create_sale(
                db,
                {item_id: 1},
                paid=1500,
                payment_type="cash",
                customer_name="سارة",
            )

            self.assertEqual(total, 1000)
            self.assertEqual(change, 500)
            customers = list_customers(db)
            self.assertEqual(len(customers), 1)
            self.assertEqual(customers[0]["name"], "سارة")
            self.assertEqual(customers[0]["sale_count"], 1)
            self.assertEqual(customers[0]["total_paid"], 1000)
            self.assertEqual(customers[0]["total_debt"], 0)

            statement = customer_sales(db, customers[0]["id"])
            self.assertEqual([row["id"] for row in statement], [sale_id])
            self.assertEqual(statement[0]["customer_name"], "سارة")

    def test_search_sales_filters_by_invoice_product_and_date(self):
        with db_session(self.db_path) as db:
            tea_id = create_item(db, "شاي", "T-1", "مشروبات", 1000, 12)
            cake_id = create_item(db, "كيك", "C-1", "حلويات", 2500, 4)
            tea_sale_id, _, _ = create_sale(db, {tea_id: 1}, paid=1000)
            cake_sale_id, _, _ = create_sale(db, {cake_id: 1}, paid=2500)
            sale_date = db.execute(
                "SELECT substr(created_at, 1, 10) FROM sales WHERE id = ?",
                (tea_sale_id,),
            ).fetchone()[0]

            self.assertEqual([row["id"] for row in search_sales(db, str(tea_sale_id))], [tea_sale_id])
            self.assertEqual([row["id"] for row in search_sales(db, product_name="كيك")], [cake_sale_id])
            self.assertEqual(
                {row["id"] for row in search_sales(db, sale_date=sale_date)},
                {tea_sale_id, cake_sale_id},
            )

    def test_login_credentials_are_taha_and_one(self):
        self.assertEqual(LOGIN_USERNAME, "طه")
        self.assertEqual(LOGIN_PASSWORD, "1")
        self.assertTrue(secure_text_equal("طه", LOGIN_USERNAME))
        self.assertFalse(secure_text_equal("taha", LOGIN_USERNAME))

    def test_admin_has_all_interfaces(self):
        with db_session(self.db_path) as db:
            self.assertEqual(user_permission_keys(db, LOGIN_USERNAME), all_interface_keys())
            self.assertTrue(user_can_access(db, LOGIN_USERNAME, "users"))
            self.assertEqual(default_path_for_user(db, LOGIN_USERNAME), "/products")

    def test_created_user_gets_selected_interfaces_only(self):
        with db_session(self.db_path) as db:
            create_user(db, "أحمد", "123", {"pos"})

            self.assertEqual(user_permission_keys(db, "أحمد"), {"pos"})
            self.assertTrue(user_can_access(db, "أحمد", "pos"))
            self.assertFalse(user_can_access(db, "أحمد", "products"))
            self.assertFalse(user_can_access(db, "أحمد", "users"))
            self.assertEqual(default_path_for_user(db, "أحمد"), "/pos")


if __name__ == "__main__":
    unittest.main()
