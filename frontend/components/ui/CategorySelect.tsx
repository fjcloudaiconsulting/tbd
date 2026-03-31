"use client";

import type { Category } from "@/lib/types";

interface Props {
  id: string;
  categories: Category[];
  value: number | "";
  onChange: (id: number | "") => void;
  filterType?: "income" | "expense";
  className?: string;
}

export default function CategorySelect({ id, categories, value, onChange, filterType, className = "" }: Props) {
  const masters = categories.filter((c) => c.parent_id === null);
  const childrenOf = (parentId: number) =>
    categories.filter((c) => c.parent_id === parentId && (!filterType || c.type === filterType || c.type === "both"));

  return (
    <select
      id={id}
      required
      value={value}
      onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
      className={className}
    >
      <option value="">Select category</option>
      {masters
        .filter((m) => !filterType || m.type === filterType || m.type === "both")
        .map((master) => {
          const subs = childrenOf(master.id);
          if (subs.length === 0) {
            return <option key={master.id} value={master.id}>{master.name}</option>;
          }
          return (
            <optgroup key={master.id} label={master.name}>
              {subs.map((sub) => (
                <option key={sub.id} value={sub.id}>{sub.name}</option>
              ))}
            </optgroup>
          );
        })}
    </select>
  );
}
