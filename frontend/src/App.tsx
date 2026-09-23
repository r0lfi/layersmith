import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Projects from "./pages/Projects";
import NewProject from "./pages/NewProject";
import ProjectDetail from "./pages/ProjectDetail";
import Builds from "./pages/Builds";
import BuildDetail from "./pages/BuildDetail";
import SettingsPage from "./pages/Settings";

export default function App() {
  return (
    <div className="app">
      <header className="header">
        <NavLink to="/" className="brand">
          <img src="/logo-mark.svg" alt="" width={34} height={34} />
          <span className="brand-text">
            <span className="brand-name">
              Layer<span>Smith</span>
            </span>
            <span className="brand-by">by xnett.org</span>
          </span>
        </NavLink>
        <nav className="nav">
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/projects">Projects</NavLink>
          <NavLink to="/builds">Builds</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
      </header>

      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/new" element={<NewProject />} />
          <Route path="/projects/:id" element={<ProjectDetail />} />
          <Route path="/builds" element={<Builds />} />
          <Route path="/builds/:id" element={<BuildDetail />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<p>Page not found.</p>} />
        </Routes>
      </main>

      <footer className="footer">
        <strong>LayerSmith</strong>
        <span>Build container images with purpose.</span>
        <span className="spacer" />
        <span>by xnett.org</span>
      </footer>
    </div>
  );
}
