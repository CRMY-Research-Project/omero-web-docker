from setuptools import setup, find_packages

setup(
    name='omero-download-gate',
    version='0.1.0dev',
    description="OMERO.web plugin gating image downloads behind an "
                "admin-reviewed approval workflow",
    author="CRM OMERO Portal Team",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Plugins",
        "Intended Audience :: Developers",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: GNU Affero General Public License v3 ",  # noqa
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    packages=find_packages(exclude=['ez_setup']),
    include_package_data=True,
    install_requires=['omero-web>=5.6.0'],
    keywords=['OMERO.web', 'download', 'approval'],
)
