export interface User {
  id: number;
  username: string;
  email: string;
  role: "owner" | "admin" | "member";
  org_id: number;
  org_name: string;
  is_superadmin: boolean;
  is_active: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface ApiError {
  detail: string;
}

export interface AccountType {
  id: number;
  name: string;
  account_count: number;
}

export interface Account {
  id: number;
  name: string;
  account_type_id: number;
  account_type_name: string;
  balance: number;
  currency: string;
  is_active: boolean;
}

export interface Category {
  id: number;
  name: string;
  type: "income" | "expense" | "both";
  transaction_count: number;
}

export interface Transaction {
  id: number;
  account_id: number;
  account_name: string;
  category_id: number;
  category_name: string;
  description: string;
  amount: number;
  type: "income" | "expense";
  date: string;
}

export interface OrgSetting {
  key: string;
  value: string;
}
