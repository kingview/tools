import multiprocessing


if __name__ == "__main__":
    multiprocessing.freeze_support()
    from media_content_analyzer.watermark_desktop import main

    main()
