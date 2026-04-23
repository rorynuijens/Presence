project('presence', version: '1.0.0')

install_data('app/presence.svg',
  install_dir: join_paths(get_option('datadir'), 'icons/hicolor/scalable/apps'),
  rename: 'io.github.presence.svg'
)

install_data('io.github.presence.desktop',
  install_dir: join_paths(get_option('datadir'), 'applications')
)

install_data('io.github.presence.metainfo.xml',
  install_dir: join_paths(get_option('datadir'), 'metainfo')
)

python = import('python')
py = python.find_installation()

py.install_sources(
  run_command('find', 'app', '-name', '*.py', check: true).stdout().strip().split('\n'),
  subdir: 'presence/app'
)
py.install_sources(
  run_command('find', 'md_to_slides', '-name', '*.py', check: true).stdout().strip().split('\n'),
  subdir: 'presence/md_to_slides'
)

install_data('app/presence-markdown.xml',
  install_dir: join_paths(get_option('datadir'), 'gtksourceview-5/styles')
)