import React from "react";
import { render, screen } from "@testing-library/react";

import Spinner from "@/components/ui/Spinner";


describe("Spinner", () => {
  it("renders an accessible loading status", () => {
    render(<Spinner />);

    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
  });
});
