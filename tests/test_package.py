import igris


def test_version_is_semver():
    parts = igris.__version__.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_package_data_covers_every_static_file():
    """Every file under igris/static must be shipped in wheels by a package-data pattern."""
    import glob
    from pathlib import Path

    import pytest

    tomllib = pytest.importorskip("tomllib")
    root = Path(__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as handle:
        patterns = tomllib.load(handle)["tool"]["setuptools"]["package-data"]["igris"]
    package_dir = root / "igris"
    shipped = set()
    for pattern in patterns:
        for match in glob.glob(pattern, root_dir=package_dir, recursive=True):
            shipped.add(Path(match).as_posix())
    static_files = sorted(
        path.relative_to(package_dir).as_posix()
        for path in (package_dir / "static").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    assert any("/vendor/" in name for name in static_files)
    missing = [name for name in static_files if name not in shipped]
    assert missing == []
