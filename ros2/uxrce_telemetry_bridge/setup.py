from setuptools import setup

name = 'uxrce_telemetry_bridge'
setup(
    name=name, version='0.2.2', packages=[name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + name]),
        ('share/' + name, ['package.xml']),
    ],
    install_requires=['setuptools', 'eclipse-zenoh>=1.7,<2'],
    extras_require={'test': ['pytest']},
    entry_points={'console_scripts': ['telemetry_bridge = uxrce_telemetry_bridge.node:main']},
)
