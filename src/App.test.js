import { render, screen } from '@testing-library/react';
import App from './App';

test('renders the JSONSQL heading', () => {
  render(<App />);
  const heading = screen.getByText(/JSONSQL - JSON to POSTGRESQL/i);
  expect(heading).toBeInTheDocument();
});
