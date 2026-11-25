import styled from "styled-components";
import { theme } from "../theme";

const Hero = styled.section`
  background: ${theme.colors.surface};
  border-radius: ${theme.radii.md};
  box-shadow: ${theme.shadows.card};
`;

export default function HomePage() {
  return (
    <Hero>
      <h1>Welcome back</h1>
      <p>Your recent orders are one click away.</p>
    </Hero>
  );
}
