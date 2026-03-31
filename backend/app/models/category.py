import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class CategoryType(str, enum.Enum):
    INCOME = "income"
    EXPENSE = "expense"
    BOTH = "both"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    parent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categories.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    slug: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    type: Mapped[CategoryType] = mapped_column(
        Enum(CategoryType, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=CategoryType.BOTH,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    parent: Mapped[Optional["Category"]] = relationship(
        remote_side="Category.id", foreign_keys="Category.parent_id",
        back_populates="children",
    )
    children: Mapped[list["Category"]] = relationship(
        back_populates="parent", foreign_keys="Category.parent_id",
    )
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="category")


SYSTEM_CATEGORIES = [
    {
        "slug": "income", "name": "Income", "type": "income",
        "description": "All sources of earnings and revenue",
        "children": [
            {"slug": "paycheck", "name": "Paycheck/Salary", "description": "Regular employment income"},
            {"slug": "side_hustles", "name": "Side Hustles", "description": "Freelance, gig work, or side business income"},
            {"slug": "bonuses", "name": "Bonuses", "description": "Performance bonuses, commissions, tips"},
            {"slug": "interest_dividends", "name": "Interest/Dividends", "description": "Bank interest, stock dividends, investment returns"},
            {"slug": "tax_refunds", "name": "Tax Refunds", "description": "Government tax refunds"},
        ],
    },
    {
        "slug": "housing", "name": "Housing", "type": "expense",
        "description": "Home-related expenses",
        "children": [
            {"slug": "rent_mortgage", "name": "Rent/Mortgage", "description": "Monthly rent or mortgage payment"},
            {"slug": "property_taxes", "name": "Property Taxes", "description": "Annual or semi-annual property taxes"},
            {"slug": "home_insurance", "name": "Home Insurance", "description": "Homeowner's or renter's insurance"},
            {"slug": "hoa_fees", "name": "HOA Fees", "description": "Homeowners association fees"},
            {"slug": "home_repairs", "name": "Repairs/Maintenance", "description": "Home repairs, renovations, upkeep"},
        ],
    },
    {
        "slug": "utilities", "name": "Utilities", "type": "expense",
        "description": "Monthly utility bills",
        "children": [
            {"slug": "electricity", "name": "Electricity", "description": "Electric power bill"},
            {"slug": "water", "name": "Water", "description": "Water and sewer bill"},
            {"slug": "gas_utility", "name": "Gas", "description": "Natural gas or heating bill"},
            {"slug": "internet", "name": "Internet", "description": "Internet service provider"},
            {"slug": "phone", "name": "Phone", "description": "Mobile or landline phone plan"},
            {"slug": "trash", "name": "Trash/Recycling", "description": "Waste collection and recycling"},
        ],
    },
    {
        "slug": "food_dining", "name": "Food & Dining", "type": "expense",
        "description": "Groceries and eating out",
        "children": [
            {"slug": "groceries", "name": "Groceries", "description": "Supermarket and grocery store purchases"},
            {"slug": "restaurants", "name": "Restaurants", "description": "Dine-in meals"},
            {"slug": "coffee_shops", "name": "Coffee Shops", "description": "Coffee, tea, and cafe visits"},
            {"slug": "fast_food", "name": "Fast Food/Takeout", "description": "Quick-service restaurants and delivery"},
        ],
    },
    {
        "slug": "transportation", "name": "Transportation", "type": "expense",
        "description": "Getting around expenses",
        "children": [
            {"slug": "fuel", "name": "Fuel/Gas", "description": "Gasoline, diesel, or EV charging"},
            {"slug": "car_payments", "name": "Car Payments", "description": "Auto loan or lease payments"},
            {"slug": "auto_insurance", "name": "Auto Insurance", "description": "Vehicle insurance premiums"},
            {"slug": "public_transit", "name": "Public Transit", "description": "Bus, train, subway fares"},
            {"slug": "car_maintenance", "name": "Maintenance/Repairs", "description": "Oil changes, tire rotations, repairs"},
            {"slug": "parking_tolls", "name": "Parking/Tolls", "description": "Parking fees and road tolls"},
        ],
    },
    {
        "slug": "health", "name": "Health & Wellness", "type": "expense",
        "description": "Medical and wellness expenses",
        "children": [
            {"slug": "health_insurance", "name": "Health Insurance", "description": "Medical insurance premiums"},
            {"slug": "doctor_visits", "name": "Doctor Visits/Copays", "description": "Medical appointments and copays"},
            {"slug": "pharmacy", "name": "Pharmacy/Meds", "description": "Prescription and over-the-counter medications"},
            {"slug": "gym", "name": "Gym Membership", "description": "Fitness center or gym fees"},
            {"slug": "dental_vision", "name": "Dental/Vision", "description": "Dental checkups and eye care"},
        ],
    },
    {
        "slug": "personal_care", "name": "Personal Care", "type": "expense",
        "description": "Personal grooming and clothing",
        "children": [
            {"slug": "haircuts", "name": "Haircuts", "description": "Barber or salon visits"},
            {"slug": "toiletries", "name": "Toiletries", "description": "Soap, shampoo, hygiene products"},
            {"slug": "clothing", "name": "Clothing", "description": "Clothes, outerwear, accessories"},
            {"slug": "shoes", "name": "Shoes", "description": "Footwear purchases"},
            {"slug": "laundry", "name": "Laundry/Dry Cleaning", "description": "Laundry services or dry cleaning"},
        ],
    },
    {
        "slug": "lifestyle", "name": "Lifestyle & Fun", "type": "expense",
        "description": "Entertainment and leisure",
        "children": [
            {"slug": "streaming", "name": "Streaming Services", "description": "Netflix, Spotify, and similar subscriptions"},
            {"slug": "entertainment", "name": "Movies/Concerts", "description": "Cinema, live shows, events"},
            {"slug": "hobbies", "name": "Hobbies", "description": "Sports, crafts, gaming, and other hobbies"},
            {"slug": "travel", "name": "Travel/Vacation", "description": "Flights, hotels, vacation expenses"},
            {"slug": "books_media", "name": "Books/Media", "description": "Books, magazines, digital media"},
        ],
    },
    {
        "slug": "financial_goals", "name": "Financial Goals", "type": "expense",
        "description": "Savings and investment contributions",
        "children": [
            {"slug": "emergency_fund", "name": "Emergency Fund", "description": "Contributions to emergency savings"},
            {"slug": "retirement", "name": "Retirement (401k/IRA)", "description": "Retirement account contributions"},
            {"slug": "general_savings", "name": "General Savings", "description": "General-purpose savings deposits"},
            {"slug": "investments", "name": "Brokerage Investments", "description": "Stock, bond, or fund purchases"},
        ],
    },
    {
        "slug": "debt", "name": "Debt Repayment", "type": "expense",
        "description": "Paying down outstanding debt",
        "children": [
            {"slug": "credit_card_debt", "name": "Credit Cards", "description": "Credit card balance payments"},
            {"slug": "student_loans", "name": "Student Loans", "description": "Education loan payments"},
            {"slug": "personal_loans", "name": "Personal Loans", "description": "Personal or payday loan payments"},
        ],
    },
    {
        "slug": "giving", "name": "Giving & Gifts", "type": "expense",
        "description": "Charitable giving and gifts",
        "children": [
            {"slug": "donations", "name": "Charitable Donations", "description": "Donations to nonprofits and causes"},
            {"slug": "gifts", "name": "Birthday/Holiday Gifts", "description": "Gifts for friends and family"},
        ],
    },
    {
        "slug": "miscellaneous", "name": "Miscellaneous", "type": "expense",
        "description": "Uncategorized and one-off expenses",
        "children": [
            {"slug": "bank_fees", "name": "Bank Fees", "description": "ATM fees, overdraft charges, service fees"},
            {"slug": "taxes_other", "name": "Taxes (Non-Property)", "description": "Income tax, sales tax, other tax payments"},
            {"slug": "uncategorized", "name": "Uncategorized/Unexpected", "description": "One-off or hard-to-classify expenses"},
        ],
    },
]
