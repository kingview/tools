import multiprocessing


if __name__ == "__main__":
    # Must run before importing Qt/Paddle/Whisper so PyInstaller can dispatch
    # multiprocessing helper processes instead of launching the GUI entrypoint.
    multiprocessing.freeze_support()
    from media_content_analyzer.desktop import main

    main()
